#!/bin/bash
# Runs the TeamCity scripts with fake curl/docker and sample %...% values. No network, no Docker needed.
#   bash ci/teamcity/tests/dry-run.sh
set -uo pipefail
KIT="$(cd "$(dirname "$0")/.." && pwd)"
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT; cd "$W"; mkdir bin
fails=0
check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (expected $3, got $2)"; fails=$((fails+1)); fi; }

trigger() {  # $1 version, $2 branch, $3 oracle
  sed -e "s#%dep\.Kotlin_KotlinDev_DeployMavenArtifacts_Nightly_kotlin_space_packages\.DeployVersion%#$1#" \
      -e 's#%build\.vcs\.number%#abc123#' -e "s#%hephaestus\.branch%#$2#" -e "s#%hephaestus\.oracle%#$3#" \
      -e 's#%teamcity\.serverUrl%#https://buildserver#' -e 's#%system\.teamcity\.buildType\.id%#X#' \
      -e 's#%teamcity\.build\.id%#1#' -e 's#%teamcity\.cloud\.serviceUser\.token%#T#' -e 's#%%#%#g' \
      "$KIT/internal-trigger/step-queue-cloud-build.sh" > t.sh
  PATH="$W/bin:$PATH" bash t.sh > out.txt 2>&1; echo $?
}
cat > bin/curl <<'C'
#!/bin/bash
prev=""; for a; do [ "$prev" = -d ] && printf '%s' "$a" > payload.json; prev=$a; done
echo '{}' > response.json; echo "${FAKE_HTTP:-200}"
C
chmod +x bin/curl
json() { python3 -c "import json;d=json.load(open('payload.json'));print($1)"; }

check "trigger: defaults"            "$(trigger 2.5.20-dev-3466 '' '')" 0
check "  no branchName"              "$(json "'branchName' in d")" False
check "  no oracle property"         "$(json "len(d['properties']['property'])")" 2
check "trigger: overrides"           "$(trigger 2.5.20-dev-3466 hephaestus/ci-foo inlining-native)" 0
check "  branchName on build object" "$(json "d['branchName']")" hephaestus/ci-foo
check "  oracle property"            "$(json "d['properties']['property'][2]['value']")" inlining-native
check "trigger: empty version fails" "$(trigger '' '' '')" 1
check "trigger: bad branch fails"    "$(trigger 2.5.20-dev-3466 main '')" 1
check "trigger: bad oracle fails"    "$(trigger 2.5.20-dev-3466 '' 'Bad Oracle')" 1
check "trigger: HTTP error fails"    "$(FAKE_HTTP=403 trigger 2.5.20-dev-3466 '' '')" 1

# Fake docker: logs each call as one line (calls from the checkpoint loop run concurrently),
# prints `[{}]` for inspect calls and exits $FAKE_RC. `info` reports a host with $FAKE_NCPU CPUs
# (default 32) and $FAKE_MEMORY bytes (default 64 GiB). The fuzz container (its arguments contain
# --duration) sleeps $FAKE_FUZZ_SLEEP seconds; the collector (-m ci.results) exits $FAKE_COLLECT_RC.
cat > bin/docker <<'D'
#!/bin/bash
echo "$*" >> docker.log
case " $* " in
  *" inspect "*) echo '[{}]'; [ -z "${FAKE_NO_IMAGE:-}" ] || exit 1; exit 0 ;;
  " info --format {{.NCPU}} {{.MemTotal}} ") echo "${FAKE_NCPU:-32} ${FAKE_MEMORY:-68719476736}"; exit 0 ;;
  " info --format {{.NCPU}} ") echo "${FAKE_NCPU:-32}"; exit 0 ;;
  *" --duration "*) sleep "${FAKE_FUZZ_SLEEP:-0}" ;;
  *" -m ci.results "*) exit "${FAKE_COLLECT_RC:-0}" ;;
esac
exit "${FAKE_RC:-0}"
D
chmod +x bin/docker
step() {  # $1 step script; the rest are environment assignments
  local script=$1; shift
  sed -e 's/%teamcity\.build\.id%/1/g' "$KIT/cloud/$script" > s.sh
  rm -f docker.log
  env "$@" PATH="$W/bin:$PATH" bash s.sh > out.txt 2>&1; echo $?
}
fuzz() {  # $1 fake docker exit code; more environment assignments may follow
  local rc=$1; shift
  step step-2-fuzz.sh FAKE_RC="$rc" HEPHAESTUS_ORACLE=inlining-js HEPHAESTUS_DURATION=23h \
    HEPHAESTUS_CHECKPOINT=1h "$@"
}
calls() { cut -d' ' -f1-2 docker.log | tr '\n' ',' ; }   # first two words of every call
fuzz_call() { grep -- '--duration' docker.log; }

check "fuzz: budget used up (0) passes"     "$(fuzz 0)" 0
check "  oracle and duration go to ci.fuzz" "$(fuzz_call | grep -o 'hephaestus:1 .*')" \
      "hephaestus:1 inlining-js --duration 23h"
check "  container kept for step 3 (no --rm)" "$(fuzz_call | grep -c -- '--rm')" 0
check "  TeamCity and heap variables passed" "$(fuzz_call | grep -o -e '-e TEAMCITY_VERSION' -e '-e HEPHAESTUS_JAVA_OPTS' | wc -l | tr -d ' ')" 2
check "  no timeout wrapper"                "$(fuzz_call | grep -c ' timeout ')" 0
check "  sized from the Docker host"        "$(fuzz_call | grep -o -- '--cpus [^ ]* --memory [^ ]*')" \
      "--cpus 32 --memory 64512m"
check "  Docker host recorded"              "$(grep -c -e '^docker_cpus=32$' -e '^fuzz_memory=64512m$' results/agent/host.env)" 2
check "  bugs/ writable by the container"   "$(stat -c %a bugs 2>/dev/null || stat -f %Lp bugs)" 777
check "  results/final writable"            "$(stat -c %a results/final 2>/dev/null || stat -f %Lp results/final)" 777
check "  image inspected for provenance"    "$(cat results/agent/image.json)" "[{}]"
check "  TeamCity facts recorded"           "$(grep -c -e '^build_id=1$' -e '^oracle=inlining-js$' results/agent/teamcity.env)" 2
check "  no checkpoint before the interval" "$(grep -c 'ci.results' docker.log)" 0
check "  no sidecar container used"         "$(grep -c 'hephaestus-1-checkpoint' docker.log)" 0
fuzz 0 FAKE_NCPU=2 FAKE_MEMORY=4294967296 >/dev/null
check "fuzz: 4 GiB host reserves 1 GiB for the agent" "$(fuzz_call | grep -o -- '--cpus [^ ]* --memory [^ ]*')" \
      "--cpus 2 --memory 3072m"
fuzz 0 HEPHAESTUS_CPUS=6 HEPHAESTUS_MEMORY=20g >/dev/null
check "fuzz: HEPHAESTUS_CPUS/MEMORY override" "$(fuzz_call | grep -o -- '--cpus [^ ]* --memory [^ ]*')" \
      "--cpus 6 --memory 20g"
check "fuzz: watchdog (124) fails"          "$(fuzz 124)" 124
check "fuzz: SIGTERM (143) fails"           "$(fuzz 143)" 143
check "fuzz: disk almost full (74) fails"   "$(fuzz 74)" 74
check "fuzz: bad oracle or duration (2) fails" "$(fuzz 2)" 2
check "  build problem reported"            "$(grep -c "buildProblem description='Fuzzer exited with code 2'" out.txt)" 1

check "fuzz: checkpoints while running"     "$(fuzz 0 HEPHAESTUS_CHECKPOINT=1s FAKE_FUZZ_SLEEP=3.5)" 0
check "  results mounted into the Fuzz container" \
      "$(fuzz_call | grep -c -- "-v $W/results:/results")" 1
check "  checkpoint runs inside the Fuzz container" \
      "$(grep -m1 'exec hephaestus-1' docker.log | grep -c 'exec python -m ci.results --checkpoint')" 1
check "  checkpoint is first OOM victim" \
      "$(grep -m1 'exec hephaestus-1' docker.log | grep -c 'echo 1000 > /proc/self/oom_score_adj')" 1
check "  checkpoint reads and writes mounted results" \
      "$(grep -m1 'exec hephaestus-1' docker.log | grep -c -- '--agent /results/agent --out \"\$1\" checkpoint /results/checkpoint-1')" 1
check "  no checkpoint sidecar launched" \
      "$(grep -c 'docker run.*ci.results' docker.log)" 0
last=$(ls -d results/checkpoint-* | sort -t- -k2 -n | tail -1)
check "  several checkpoints ran"           "$([ "${last##*-}" -ge 2 ] && echo yes)" yes
check "  only the latest checkpoint kept"   "$(ls -d results/checkpoint-* | wc -l | tr -d ' ') ${last##*-}" \
      "1 $(grep -c '^##teamcity\[publishArtifacts .*/hephaestus-results.zip => checkpoint' out.txt)"
check "  checkpoint folder made writable first" \
      "$(stat -c %a "$last" 2>/dev/null || stat -f %Lp "$last")" 777
check "  published under checkpoint/" \
      "$(grep -c "^##teamcity\[publishArtifacts 'results/checkpoint-1/hephaestus-results.zip => checkpoint'\]$" out.txt)" 1
check "  summary and provenance too" \
      "$(grep -c -e "checkpoint-1/summary.json => checkpoint" -e "checkpoint-1/provenance.json => checkpoint" out.txt)" 2
check "  no sidecar container used"         "$(grep -c 'hephaestus-1-checkpoint' docker.log)" 0
check "fuzz: failed checkpoint only warns"  "$(fuzz 0 HEPHAESTUS_CHECKPOINT=1s FAKE_FUZZ_SLEEP=1.5 FAKE_COLLECT_RC=1)" 0
check "  warning, nothing published"        "$(grep -c -e "Checkpoint 1 failed" -e "publishArtifacts" out.txt)" 1
check "  failed checkpoint folder removed"  "$(ls -d results/checkpoint-* 2>/dev/null | wc -l | tr -d ' ')" 0

mkdir -p bugs results
check "collect: passes"                     "$(step step-3-collect.sh)" 0
check "  stop, inspect, collect in order"    "$(calls)" \
      "stop --time,image inspect,inspect hephaestus-1,info --format,run --rm,"
check "  collector reads the agent facts" \
      "$(grep -- '-m ci.results' docker.log | grep -o 'hephaestus:1 .*')" \
      "hephaestus:1 -m ci.results --agent /agent --out /out --teamcity"
check "  container state saved"             "$(cat results/agent/container.json)" "[{}]"
check "  collector gets two CPUs"           "$(grep -- '-m ci.results' docker.log | grep -o -- '--cpus [^ ]*')" "--cpus 2"
step step-3-collect.sh FAKE_NCPU=1 >/dev/null
check "collect: one CPU on a one-CPU host"  "$(grep -- '-m ci.results' docker.log | grep -o -- '--cpus [^ ]*')" "--cpus 1"
check "  no fallback"                       "$(grep -c publishArtifacts out.txt)" 0
check "collect: failure publishes the JSON" "$(step step-3-collect.sh FAKE_COLLECT_RC=1)" 1
check "  JSON files published"              "$(grep -c "publishArtifacts 'bugs/\*/\*.json => hephaestus-results-json.zip'" out.txt)" 1
check "collect: no image is only a warning" "$(step step-3-collect.sh FAKE_NO_IMAGE=1)" 0
check "  nothing collected"                 "$(grep -c 'ci.results' docker.log)" 0

check "cleanup: passes even if docker fails" "$(step step-4-cleanup.sh FAKE_RC=1)" 0
check "cleanup: passes"                     "$(step step-4-cleanup.sh)" 0
check "  chown, containers, image in order" "$(calls)" \
      "image inspect,run --rm,run --rm,rm -f,image rm,"
check "  bugs/ and results/ handed back" \
      "$(grep -c -e "-v $W/bugs:/w hephaestus:1 -R $(id -u):$(id -g) /w" -e "-v $W/results:/w hephaestus:1 -R $(id -u):$(id -g) /w" docker.log)" 2
check "  results/final kept for the artifact rules" "$([ -d results/final ] || [ ! -e results/final ] && echo kept)" kept

# The .kts embeds the step scripts with every bash $ written as ${'$'}.
KTS="$KIT/cloud/HephaestusNightlyFuzzing.kts"
for n in 1 2 3 4; do
  script=$(ls "$KIT"/cloud/step-$n-*.sh)
  embedded=$(awk -v n=$n '/scriptContent = """/ {i++; if (i == n) {on = 1; next}} on && /"""\.trimIndent\(\)/ {exit} on' \
             "$KTS" | sed -e 's/^            //' -e "s/\${'\\$'}/\$/g")
  check "kts embeds $(basename "$script")" "$([ "$embedded" = "$(cat "$script")" ] && echo same || echo differs)" same
done
check "kts publishes artifacts even when stopped" "$(grep -c 'publishArtifacts = PublishMode.ALWAYS' "$KTS")" 1
check "kts publishes results/final only"   "$(grep -c -e '^        results/final/' -e 'bugs => ' "$KTS")" 3
check "kts runs steps 3 and 4 always"      "$(grep -c 'executionMode = BuildStep.ExecutionMode.ALWAYS' "$KTS")" 2

for f in "$KIT"/{cloud,internal-trigger,rest,local}/*.sh; do
  if grep -v '^[[:space:]]*#' "$f" | grep -q '<HEPHAESTUS_FUZZING_BUILD_ID>'; then
    echo "todo $(basename "$f") still has placeholders to fill in"
  fi
  bash -n "$f" && echo "ok   syntax $(basename "$f")" || { echo "FAIL syntax $f"; fails=$((fails+1)); }
done
[ $fails -eq 0 ] && echo "All checks passed." || { echo "$fails check(s) failed."; exit 1; }
