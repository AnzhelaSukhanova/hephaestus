from __future__ import annotations

from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from typing import Union

from src import utils
from src.ir import ast, types
from src.ir.data_structures import StackWithCounter

class Context():

    def __init__(self):
        # CROSSMODULE SUPPORT
        self._target_module = None # klib for which this Context() is produced

        # _context and _namespaces are tied together
        # _context[namespace] : declarations in scope
        # _namespaces[decl] : scope with a given declaration
        self._context = {}
        # A lookup from declarations to namespaces
        self._namespaces = {}

        # LOCAL TO AST SUBTREE
        # Cleared when we temporarily go to other AST subtree (to general global helper function for example). This is the main one
        self._subtree_call_stack = StackWithCounter()
        # Optimization on top of self._subtree_call_stack to get O(1) access to last call of each type
        self._typed_subtree_call_stacks = defaultdict(list)

        # PERSISTENT
        # Persists during AST subtree jumps (inside of global function generation)
        self._persistent_call_stack = StackWithCounter()
        # Optimization on top of self._persistent_call_stack to get O(1) access to last call of each type
        self._typed_persistent_call_stacks = defaultdict(list)

    @property
    def target_module(self):
        return getattr(self, '_target_module', None)

    @target_module.setter
    def target_module(self, module):
        self._target_module = module

    def name_is_local(self, name: str) -> bool:
        """Is ``name`` from this context's module"""
        if not self.target_module:
            return True
        name = str(name)
        if '.' not in name:
            return True
        module, _ = utils.split_qualified_name(name)
        return module == self.target_module

    # HELPERS TO WORK WITH ANY STACKED CONTEXT
    def _push_call_context(self, callcontext, call_stack, typed_call_stacks):
        call_stack.append(callcontext)
        typed_call_stacks[type(callcontext)].append(callcontext)

    def _pop_call_context(self, call_stack, typed_call_stacks):
        if not call_stack:
            return None
        last_call_context = call_stack.pop()
        assert typed_call_stacks[type(last_call_context)].pop() is last_call_context
        return last_call_context

    def _has_call_context(self, frame_type, typed_call_stacks):
        return bool(typed_call_stacks.get(frame_type, []))

    def _current_call_context(self, call_stack, typed_call_stacks, frame_type=None):
        if frame_type is None:
            if not call_stack:
                return None
            return call_stack[-1]

        if self._has_call_context(frame_type, typed_call_stacks):
            return typed_call_stacks[frame_type][-1]
        else:
            return None

    # AST SUBTREE CALL STACK
    def push_call_context(self, callcontext):
        self._push_call_context(callcontext, self._subtree_call_stack, self._typed_subtree_call_stacks)

    def pop_call_context(self):
        return self._pop_call_context(self._subtree_call_stack, self._typed_subtree_call_stacks)

    def has_call_context(self, frame_type):
        return self._has_call_context(frame_type, self._typed_subtree_call_stacks)

    def current_call_context(self, frame_type=None):
        return self._current_call_context(self._subtree_call_stack, self._typed_subtree_call_stacks, frame_type)

    # SUBTREE STACK SUFFIX, TAIL
    def call_context_stack_suffix_types(self, *frame_types) -> bool:
        stack = self._subtree_call_stack
        n = len(frame_types)

        if len(stack) < n:
            return False

        suffix = stack[-n:]
        return all(isinstance(frame, expected)
                   for frame, expected in zip(suffix, frame_types))

    def call_context_tail(self, limit: int = 4):
        return tuple(self._subtree_call_stack[-limit:])

    # PERSISTENT CALL STACK
    def push_persistent_call_context(self, callcontext):
        self._push_call_context(callcontext, self._persistent_call_stack, self._typed_persistent_call_stacks)

    def pop_persistent_call_context(self):
        return self._pop_call_context(self._persistent_call_stack, self._typed_persistent_call_stacks)

    def has_persistent_call_context(self, frame_type):
        return self._has_call_context(frame_type, self._typed_persistent_call_stacks)

    def current_persistent_call_context(self, frame_type=None):
        return self._current_call_context(self._persistent_call_stack, self._typed_persistent_call_stacks, frame_type)

    # GLOBAL CONTEXT MANAGEMENT
    # Subtree isolation
    @contextmanager
    def isolate_call_stacks(self):
        initial_call_stack = self._subtree_call_stack
        initial_typed_call_stacks = self._typed_subtree_call_stacks

        self._subtree_call_stack = []
        self._typed_subtree_call_stacks = defaultdict(list)
        try:
            yield
            assert not self._subtree_call_stack, "Must pop everything out"
        finally:
            self._subtree_call_stack = initial_call_stack
            self._typed_subtree_call_stacks = initial_typed_call_stacks

    @contextmanager
    def call_contexts(self, subtree_pushed_call_context=(), persistent_pushed_call_context=()):
        subtree_pushed_call_context = [frame for frame in subtree_pushed_call_context if frame is not None]
        persistent_pushed_call_context = [frame for frame in persistent_pushed_call_context if frame is not None]

        for frame in subtree_pushed_call_context:
            self.push_call_context(frame)
        for frame in persistent_pushed_call_context:
            self.push_persistent_call_context(frame)

        try:
                yield
        finally:
            for expected in reversed(persistent_pushed_call_context):
                assert self.pop_persistent_call_context() is expected
            for expected in reversed(subtree_pushed_call_context):
                assert self.pop_call_context() is expected


    def _add_entity(self, namespace, entity, name,
                    value: Union[ast.Declaration, types.TypeParameter,
                                 ast.Lambda]):
        if namespace in self._context:
            self._context[namespace][entity][name] = value
        else:
            self._context[namespace] = {
                'types': {},
                'funcs': {},
                'lambdas': {},
                'vars': {},
                'classes': {},
                'decls': OrderedDict()  # Here we keep the declaration order
            }
            self._context[namespace][entity][name] = value
        self._namespaces[value] = namespace

    def _add_declaration_entity(self, namespace, entity, name,
                                value: ast.Declaration):
        self._add_entity(namespace, entity, name, value)
        self._add_entity(namespace, 'decls', name, value)

    def update_declarations(self, decls):
        self._context[ast.GLOBAL_NAMESPACE]['decls'] = decls

    def _add_function(self, namespace, func):
        self.add_func(namespace, func.name, func)
        namespace = namespace + (func.name,)
        for param in func.params:
            self.add_var(namespace, param.name, param)
        if not func.body or not isinstance(func.body, ast.Block):
            return
        stack = list(func.body.body)
        while stack:
            statement = stack.pop()
            if isinstance(statement, ast.VariableDeclaration):
                self.add_var(namespace, statement.name, statement)

            if isinstance(statement, ast.FunctionDeclaration):
                self._add_function(namespace, statement)

            if isinstance(statement, ast.Conditional):
                stack.append(statement.true_branch)
                stack.append(statement.false_branch)

            if isinstance(statement, ast.Block):
                stack.extend(statement.body)

    def _add_class(self, namespace, class_decl):
        namespace = namespace + (class_decl.name,)
        for field in class_decl.fields:
            self.add_var(namespace, field.name, field)
        for function in class_decl.functions:
            self._add_function(namespace, function)

    def add_declaration(self, decl):
        decl_types = {
            ast.FunctionDeclaration: self.add_func,
            ast.ClassDeclaration: self.add_class,
            ast.VariableDeclaration: self.add_var,
        }
        decl_types[decl.__class__](ast.GLOBAL_NAMESPACE, decl.name, decl)
        if isinstance(decl, ast.ClassDeclaration):
            self._add_class(ast.GLOBAL_NAMESPACE, decl)

        if isinstance(decl, ast.FunctionDeclaration):
            self._add_function(ast.GLOBAL_NAMESPACE, decl)

    def _remove_entity(self, namespace, entity, name):
        if namespace not in self._context:
            return
        if name in self._context[namespace][entity]:
            decl = self._context[namespace][entity][name]
            if decl in self._namespaces:
                del self._namespaces[decl]
            del self._context[namespace][entity][name]

    def remove_declaration(self, decl):
        decl_types = {
            ast.FunctionDeclaration: self.remove_func,
            ast.ClassDeclaration: self.remove_class,
            ast.VariableDeclaration: self.remove_var,
        }
        decl_types[decl.__class__](ast.GLOBAL_NAMESPACE, decl.name)

    def prepare_this_context_for_import(self):
        """Prepare this completed context for use as an imported dependency"""
        self._drop_unreachable_global_declarations_from_lookup()

    def _drop_unreachable_global_declarations_from_lookup(self):
        """Remove entry points to global declarations that fail to
         cross cross-module boundary. Their namespaces still survive
         but lack entry points for access
        """
        global_entities = self._context[ast.GLOBAL_NAMESPACE]

        unreachable = [
            decl for decl in global_entities["decls"].values()
            if not decl.can_cross_module_boundary()
        ]
        for decl in unreachable:
            self.remove_declaration(decl)

    def add_type(self, namespace, type_name, t: types.TypeParameter):
        self._add_entity(namespace, 'types', type_name, t)

    def add_func(self, namespace, func_name,
                 func: ast.FunctionDeclaration):
        self._add_declaration_entity(namespace, 'funcs', func_name, func)

    def add_lambda(self, namespace, shadow_name, lmd: ast.Lambda):
        self._add_entity(namespace, 'lambdas', shadow_name, lmd)

    def add_var(self, namespace, var_name,
                var: Union[ast.VariableDeclaration, ast.FieldDeclaration,
                           ast.ParameterDeclaration]):
        self._add_declaration_entity(namespace, 'vars', var_name, var)

    def add_class(self, namespace, class_name,
                  cls: ast.ClassDeclaration):
        self._add_declaration_entity(namespace, 'classes', class_name, cls)

    def remove_type(self, namespace, type_name):
        self._remove_entity(namespace, 'types', type_name)

    def remove_var(self, namespace, var_name):
        self._remove_entity(namespace, 'vars', var_name)
        self._remove_entity(namespace, 'decls', var_name)

    def remove_func(self, namespace, func_name):
        self._remove_entity(namespace, 'funcs', func_name)
        self._remove_entity(namespace, 'decls', func_name)

    def remove_lambda(self, namespace, shadow_name):
        self._remove_entity(namespace, 'lambdas', shadow_name)

    def remove_class(self, namespace, class_name):
        self._remove_entity(namespace, 'classes', class_name)
        self._remove_entity(namespace, 'decls', class_name)

    def _get_declarations_glob(self, namespace, decl_type):
        decls = OrderedDict({})
        namespaces = [(namespace[0],)]
        while namespaces:
            namespace = namespaces.pop()
            decl = self._context.get(namespace, {}).get(decl_type)
            if decl is not None:
                decls.update(decl)
            namespaces.extend(self.find_namespaces(namespace, True))
        return decls

    def _get_declarations(self, namespace, decl_type, only_current, glob, none):
        len_namespace = len(namespace)
        assert len_namespace >= 1
        decls = {}
        if glob:
            decls = self._get_declarations_glob(namespace, decl_type)
        elif len_namespace == 1 or only_current:
            decls = self._context.get(namespace, {}).get(decl_type, {})
        else:
            start = (namespace[0],)
            decls = OrderedDict(self._context.get(start, {}).get(decl_type) or {})
            for ns in namespace[1:]:
                start = start + (ns,)
                decl = self._context.get(start, {}).get(decl_type)
                if decl is not None:
                    decls.update(decl)
        if not none:
            # Do not return artificial nodes
            decls = {k: v for k, v in decls.items() if v is not None}
        return decls

    def find_namespaces(self, namespace, none):
        func_namespaces = [namespace + (fname,)
                           for fname in self.get_funcs(namespace, True, none=none)]
        class_namespaces = [namespace + (cname,)
                            for cname in self.get_classes(namespace, True, none=none)]
        return func_namespaces + class_namespaces

    def get_namespaces_decls(self, namespace, name, decl_type, glob=True):
        """Return a set of tuples of namespace, decl. Note that namespace
        includes the name of the decl.
        """
        namespaces_decls = set()  # Set of tuples of namespace, decl
        if glob:
            namespaces = [(namespace[0],)]
        else:
            namespaces = [namespace]
        while namespaces:
            namespace = namespaces.pop()
            decls = self._context.get(namespace, {}).get(decl_type)
            if decls is not None:
                for decl_name, decl in decls.items():
                    if decl_name == name:
                        ns = namespace + (name,)
                        namespaces_decls.add((ns, decl))
            namespaces.extend(self.find_namespaces(namespace, none=False))
        return namespaces_decls

    def get_decl(self, namespace, name):
        return self._context.get(namespace, {}).get('decls', {}).get(
            name, None)

    def get_lambda(self, namespace, name):
        return self._context.get(namespace, {}).get('lambdas', {}).get(
            name, None)

    def get_types(self, namespace, only_current=False, glob=False, none=False):
        return self._get_declarations(namespace, 'types', only_current, glob, none)

    def get_funcs(self, namespace, only_current=False, glob=False, none=False):
        return self._get_declarations(namespace, 'funcs', only_current, glob, none)

    def get_lambdas(self, namespace, only_current=False, glob=False, none=False):
        return self._get_declarations(namespace, 'lambdas', only_current, glob, none)

    def get_vars(self, namespace, only_current=False, glob=False, none=False):
        return self._get_declarations(namespace, 'vars', only_current, glob, none)

    def get_classes(self, namespace, only_current=False, glob=False, none=False):
        return self._get_declarations(namespace, 'classes', only_current, glob, none)

    def get_declarations(self, namespace, only_current=False, glob=False, none=False):
        return self._get_declarations(namespace, 'decls', only_current, glob, none)

    def remove_namespace(self, namespace):
        if namespace in self._context:
            self._context.pop(namespace)

    def get_declarations_in(self, namespace):
        decls = {}
        for ns, entities in self._context.items():
            if utils.prefix_lst(namespace, ns):
                decls[ns] = entities['decls']
        return decls

    def get_decl_type(self, namespace, name):
        return type(self.get_decl(namespace, name))

    def get_namespace(self, decl):
        return self._namespaces.get(decl, None)

    def get_parent(self, namespace):
        if len(namespace) < 2:
            return None
        parent_namespace = namespace[:-1]
        return self.get_decl(parent_namespace[:-1], parent_namespace[-1])

    def get_parent_class(self, namespace):
        parent = self.get_parent(namespace)
        if parent is None and not (len(namespace) > 2 and
                                   'lambda_' in namespace[-2]):
            return None
        if isinstance(parent, ast.ClassDeclaration):
            return parent
        return self.get_parent_class(namespace[:-1])


def get_decl(context, namespace, decl_name: str, limit=None):
    """
    We search the context for a declaration with the given name (`decl_name`).

    The search begins from the given namespace `namespace` up to the namespace
    given by `limit`.
    """
    def stop_cond(ns):
        # If 'limit' is provided, we search the given declaration 'node'
        # up to a certain namespace.
        return (len(ns)
                if limit is None
                else utils.prefix_lst(limit, ns))

    while stop_cond(namespace):
        decl = context.get_decl(namespace, decl_name)
        if decl:
            return namespace, decl
        namespace = namespace[:-1]
    return None
