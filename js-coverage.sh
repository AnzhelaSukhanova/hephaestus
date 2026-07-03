#!/bin/sh
rm -rf coverage/html coverage/report.xml coverage/report.csv

CLASSES='InlineCallCycleCheckerLowering|LocalClassesInInlineLambdasLowering|PreSerializationPrivateFunctionInlining|InlineDeclarationCheckerLowering|OuterThisInInlineFunctionsSpecialAccessorLowering|CommonLoweringPhasesKt|SyntheticAccessorLowering|IrValidationAfterInliningOnlyPrivateFunctionsPhase|FunctionInlining|InlineFunctionSerializationPreProcessing|IrValidationAfterInliningAllFunctionsOnTheFirstStagePhase'

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/out" "$TMP/classes"

for TEST in bugs/coverage/*.kt
do
  NAME="$(basename "$TEST" .kt)"
  echo "$TEST"

  "$HOME/kotlin/dist/kotlinc/bin/kotlinc-js" "$TEST" \
    -Xreport-all-warnings \
    -Xklib-ir-inliner=intra-module \
    -Xverify-ir=warning \
    -nopack \
    -ir-output-dir "$TMP/out/$NAME" \
    -ir-output-name "$NAME" \
    -libraries "$HOME/kotlin/dist/kotlinc/lib/kotlin-stdlib-js.klib" \
    "-J-javaagent:coverage/jacocoagent.jar=destfile=coverage/jacoco.exec,append=true,inclnolocationclasses=true,includes=org/jetbrains/kotlin/backend/common/lower/*:org/jetbrains/kotlin/backend/common/lower/inline/*:org/jetbrains/kotlin/ir/inline/*:org/jetbrains/kotlin/backend/common/phaser/*" \
    > "$TMP/$NAME.log" 2>&1

  cat "$TMP/$NAME.log"
done

jar tf "$HOME/kotlin/dist/kotlinc/lib/kotlin-compiler.jar" \
  | grep -E "^(org/jetbrains/kotlin/backend/common/lower|org/jetbrains/kotlin/backend/common/lower/inline|org/jetbrains/kotlin/ir/inline|org/jetbrains/kotlin/backend/common/phaser)/($CLASSES).*\\.class$" \
  > "$TMP/classes.list"

(
  cd "$TMP/classes"
  xargs jar xf "$HOME/kotlin/dist/kotlinc/lib/kotlin-compiler.jar" < "$TMP/classes.list"
)

java -jar coverage/jacococli.jar report coverage/jacoco.exec \
  --classfiles "$TMP/classes" \
  --sourcefiles "$HOME/kotlin/compiler/ir/backend.common/src" \
  --sourcefiles "$HOME/kotlin/compiler/ir/ir.inline/src" \
  --html coverage/html \
  --xml coverage/report.xml \
  --csv coverage/report.csv
