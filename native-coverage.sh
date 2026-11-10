#!/bin/sh
rm -rf coverage/html coverage/report.xml coverage/report.csv

CLASSES='InlineCallCycleCheckerLowering|LocalClassesInInlineLambdasLowering|PreSerializationPrivateFunctionInlining|InlineDeclarationCheckerLowering|OuterThisInInlineFunctionsSpecialAccessorLowering|CommonLoweringPhasesKt|SyntheticAccessorLowering|IrValidationAfterInliningOnlyPrivateFunctionsPhase|FunctionInlining|InlineFunctionSerializationPreProcessing|IrValidationAfterInliningAllFunctionsOnTheFirstStagePhase'

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/classes"

for TEST in bugs/coverage/*.kt
do
  NAME="$(basename "$TEST" .kt)"
  echo "$TEST"

  "$HOME/kotlin/kotlin-native/dist/bin/kotlinc-native" "$TEST" \
    -Xreport-all-warnings \
    -Xverify-ir=warning \
    -produce library \
    -o "$TMP/$NAME" \
    -nowarn \
    "-J-javaagent:coverage/jacocoagent.jar=destfile=coverage/jacoco.exec,append=true,inclnolocationclasses=true,includes=org/jetbrains/kotlin/backend/common/lower/*:org/jetbrains/kotlin/backend/common/lower/inline/*:org/jetbrains/kotlin/ir/inline/*:org/jetbrains/kotlin/backend/common/phaser/*:org/jetbrains/kotlin/backend/konan/*" \
    > "$TMP/$NAME.log" 2>&1

  cat "$TMP/$NAME.log"
done

NATIVE_COMPILER="$HOME/kotlin/kotlin-native/dist/konan/lib/kotlin-native-compiler-embeddable.jar"

jar tf "$NATIVE_COMPILER" \
  | grep -E "^(org/jetbrains/kotlin/backend/common/lower|org/jetbrains/kotlin/backend/common/lower/inline|org/jetbrains/kotlin/ir/inline|org/jetbrains/kotlin/backend/common/phaser|org/jetbrains/kotlin/backend/konan/lower|org/jetbrains/kotlin/backend/konan/driver/phases)/($CLASSES).*\\.class$" \
  > "$TMP/classes.list"

(
  cd "$TMP/classes"
  xargs jar xf "$NATIVE_COMPILER" < "$TMP/classes.list"
)

java -jar coverage/jacococli.jar report coverage/jacoco.exec \
  --classfiles "$TMP/classes" \
  --sourcefiles "$HOME/kotlin/compiler/ir/backend.common/src" \
  --sourcefiles "$HOME/kotlin/compiler/ir/ir.inline/src" \
  --sourcefiles "$HOME/kotlin/kotlin-native/backend.native/compiler/ir/backend.native/src" \
  --html coverage/html \
  --xml coverage/report.xml \
  --csv coverage/report.csv
