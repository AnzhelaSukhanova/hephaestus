// Complete TeamCity Cloud settings for "Hephaestus nightly fuzzing" (Kotlin DSL).
// Register both objects in the Hephaestus project:  vcsRoot(HephaestusRepo); buildType(HephaestusNightlyFuzzing)
// Generated from ci/teamcity/cloud/step-*.sh: every bash `$` is written as ${'$'} so Kotlin doesn't interpolate it.
// %...% references are TeamCity parameters and stay as they are.
import jetbrains.buildServer.configs.kotlin.*
import jetbrains.buildServer.configs.kotlin.buildSteps.script
import jetbrains.buildServer.configs.kotlin.vcs.GitVcsRoot

object HephaestusRepo : GitVcsRoot({
    name = "Hephaestus"
    url = "https://github.com/AnzhelaSukhanova/hephaestus.git"
    branch = "refs/heads/hephaestus/ci-main"          // default branch = what nightly runs use
    branchSpec = """
        #! fallbackToDefault: false
        +:refs/heads/(hephaestus/ci-*)
    """.trimIndent()
})

object HephaestusNightlyFuzzing : BuildType({
    name = "Hephaestus nightly fuzzing"
    description = "Fuzzes one Kotlin nightly for HEPHAESTUS_DURATION. Queued over REST by the internal trigger."
    maxRunningBuilds = 1
    // Written by step 3 "Collect results"; checkpoints are published under checkpoint/ by step 2.
    artifactRules = """
        results/final/hephaestus-results.zip
        results/final/summary.json
        results/final/provenance.json
    """.trimIndent()
    // Also for stopped and timed-out builds (the default skips interrupted ones)
    publishArtifacts = PublishMode.ALWAYS

    vcs {
        root(HephaestusRepo)
        cleanCheckout = true
    }

    params {
        text("env.KOTLIN_MASTER_VERSION", "",
            label = "Kotlin master version",
            description = "Exact dev version, e.g. 2.5.20-dev-3466. Empty = latest prerelease.",
            allowEmpty = true)
        text("env.KOTLIN_COMMIT", "",
            label = "Kotlin commit",
            display = ParameterDisplay.HIDDEN,
            allowEmpty = true)
        select("env.HEPHAESTUS_ORACLE", "backend-comparison",
            label = "Oracle",
            display = ParameterDisplay.PROMPT,
            options = listOf(
                "Backend comparison" to "backend-comparison",
                "Inlining comparison, Native" to "inlining-native",
                "Inlining comparison, JS" to "inlining-js",
                "Inlining comparison, Wasm" to "inlining-wasm"))
        text("env.HEPHAESTUS_DURATION", "23h",
            label = "Fuzzing time",
            description = "Time budget for python -m ci.fuzz, e.g. 23h or 90m",
            regex = """\d+[smhd]""",
            validationMessage = "Use a number with s, m, h or d, e.g. 23h")
        text("env.HEPHAESTUS_CHECKPOINT", "3h",
            label = "Checkpoint interval",
            description = "How often the results archive is packed and published under checkpoint/ while fuzzing",
            regex = """\d+[smhd]""",
            validationMessage = "Use a number with s, m, h or d, e.g. 3h")
        text("env.HEPHAESTUS_CPUS", "",
            label = "Fuzz container CPUs",
            description = "Empty = every CPU of the agent's Docker host. ci.fuzz runs one worker per performance core (or CPU on uniform hosts), memory permitting",
            regex = """(\d+(\.\d+)?)?""",
            validationMessage = "Use a number of CPUs, e.g. 8, or leave it empty",
            allowEmpty = true)
        text("env.HEPHAESTUS_MEMORY", "",
            label = "Fuzz container memory",
            description = "Empty = the Docker host's memory minus 1 GB for the agent (half on hosts with 2 GB or less). Checkpoints run inside the Fuzz container; ci.fuzz sizes workers and compiler heap from this limit",
            regex = """(\d+[kmg])?""",
            validationMessage = "Use a number with k, m or g, e.g. 24g, or leave it empty",
            allowEmpty = true)
    }

    steps {
        script {
            name = "Build image"
            scriptContent = """
            #!/bin/bash
            # Cloud: step 1 "Build image" (runs in the checkout of the selected Hephaestus branch).
            set -euo pipefail
            echo "kt=${'$'}{KOTLIN_MASTER_VERSION:-<prerelease>} oracle=${'$'}HEPHAESTUS_ORACLE branch=%teamcity.build.branch% commit=%build.vcs.number%"

            docker build --no-cache --platform linux/amd64 --target runtime \
              --build-arg KOTLIN_MASTER_VERSION="${'$'}KOTLIN_MASTER_VERSION" \
              --build-arg KOTLIN_COMMIT="${'$'}KOTLIN_COMMIT" \
              --build-arg HEPHAESTUS_COMMIT="%build.vcs.number%" \
              --build-arg HEPHAESTUS_BRANCH="%teamcity.build.branch%" \
              -t "hephaestus:%teamcity.build.id%" .
            """.trimIndent()
        }
        script {
            name = "Fuzz"
            scriptContent = """
            #!/bin/bash
            # Cloud: step 2 "Fuzz". Runs the image's entrypoint, `python -m ci.fuzz <oracle> --duration <d>`.
            # ci.fuzz rejects unknown oracles and durations (exit 2), stops by itself when the time budget
            # runs out (exit 0) and leaves a complete bugs/<name>/. 143 = stopped by SIGTERM, 124 = watchdog
            # (still running 15 minutes past the budget); both still leave complete results.
            # Every HEPHAESTUS_CHECKPOINT, `docker exec` packs the live bugs/ with
            # `python -m ci.results --checkpoint` and the archive is published under checkpoint/.
            # The collector shares the Fuzz container's limits and is the first OOM-kill candidate.
            # The Fuzz container gets every CPU and all but 1 GiB of the Docker host's memory for the
            # TeamCity agent (HEPHAESTUS_CPUS / HEPHAESTUS_MEMORY override that); ci.fuzz sizes workers
            # and the compiler heap from those limits.
            set -euo pipefail
            NAME="hephaestus-%teamcity.build.id%"
            IMAGE="hephaestus:%teamcity.build.id%"

            # Containers run as the non-root `fuzzer` user, so every mounted folder must be writable by it
            rm -rf bugs results && mkdir -p bugs results/agent results/final && chmod a+rwx bugs results/final

            # Facts only the agent knows, for provenance.json (read by ci.results through --agent)
            cat > results/agent/teamcity.env <<EOF
            server=%teamcity.serverUrl%
            build_id=%teamcity.build.id%
            build_number=%build.number%
            build_type=%system.teamcity.buildType.id%
            build_url=%teamcity.serverUrl%/buildConfiguration/%system.teamcity.buildType.id%/%teamcity.build.id%
            branch=%teamcity.build.branch%
            commit=%build.vcs.number%
            agent=%teamcity.agent.name%
            oracle=${'$'}HEPHAESTUS_ORACLE
            duration=${'$'}HEPHAESTUS_DURATION
            checkpoint_interval=${'$'}HEPHAESTUS_CHECKPOINT
            EOF
            # The Docker host, not the agent's shell: under Colima or Docker Desktop that is the VM
            if ! read -r docker_cpus docker_memory < <(docker info --format '{{.NCPU}} {{.MemTotal}}'); then
              echo "##teamcity[buildProblem description='docker info failed: cannot size the Fuzz container']"
              exit 1
            fi
            docker_mib=${'$'}((docker_memory / 1048576))
            # 1 GiB for the TeamCity agent; half of hosts with 2 GiB or less
            reserve_mib=${'$'}((docker_mib > 2048 ? 1024 : docker_mib / 2))
            CPUS="${'$'}{HEPHAESTUS_CPUS:-${'$'}docker_cpus}"
            MEMORY="${'$'}{HEPHAESTUS_MEMORY:-${'$'}((docker_mib - reserve_mib))m}"
            cat > results/agent/host.env <<EOF
            kernel=${'$'}(uname -sr)
            cpus=${'$'}(nproc 2>/dev/null)
            memory_kb=${'$'}(awk '/MemTotal/ {print ${'$'}2}' /proc/meminfo 2>/dev/null)
            docker_cpus=${'$'}docker_cpus
            docker_memory_bytes=${'$'}docker_memory
            fuzz_cpus=${'$'}CPUS
            fuzz_memory=${'$'}MEMORY
            EOF
            echo "Fuzz container: ${'$'}CPUS CPUs, ${'$'}MEMORY memory (Docker host: ${'$'}docker_cpus CPUs, ${'$'}{docker_mib}m)"
            docker image inspect "${'$'}IMAGE" > results/agent/image.json || rm -f results/agent/image.json

            checkpoints() {
              # Children run in the background and are waited for, so TERM is handled at once and
              # no `sleep` is left holding the build log open after the step.
              local n=0 child="" last=""
              trap '[ -n "${'$'}child" ] && kill "${'$'}child" 2>/dev/null; exit 0' TERM
              while :; do
                sleep "${'$'}HEPHAESTUS_CHECKPOINT" & child=${'$'}!
                wait "${'$'}child"
                n=${'$'}((n + 1))
                local out="results/checkpoint-${'$'}n"
                # Created here: a folder Docker creates for a mount belongs to root
                mkdir -p "${'$'}out" && chmod a+rwx "${'$'}out"
                # The collector shares the Fuzz container's memory cgroup. Raise its OOM score so
                # memory pressure kills this checkpoint before a compiler or the fuzzer itself.
                docker exec "${'$'}NAME" sh -c \
                  'echo 1000 > /proc/self/oom_score_adj && exec python -m ci.results --checkpoint --agent /results/agent --out "${'$'}1"' \
                  checkpoint "/${'$'}out" & child=${'$'}!
                if wait "${'$'}child"; then
                  # Leading newline: the fuzzer's output may have left a line unfinished
                  printf "\n##teamcity[publishArtifacts '%s/hephaestus-results.zip => checkpoint']\n" "${'$'}out"
                  printf "##teamcity[publishArtifacts '%s/summary.json => checkpoint']\n" "${'$'}out"
                  printf "##teamcity[publishArtifacts '%s/provenance.json => checkpoint']\n" "${'$'}out"
                  # Only the latest is kept on the agent: the previous one was published an interval
                  # ago, while the latest may still be uploading
                  [ -z "${'$'}last" ] || rm -rf "${'$'}last"
                  last=${'$'}out
                else
                  printf "\n##teamcity[message text='Checkpoint %s failed' status='WARNING']\n" "${'$'}n"
                  rm -rf "${'$'}out"
                fi
              done
            }
            checkpoints &
            loop=${'$'}!

            set +e
            # No --rm: step 3 reads the container's exit code and start/end times for provenance.json.
            # TEAMCITY_VERSION turns on service messages (progress, statistics, status);
            # HEPHAESTUS_JAVA_OPTS, when set on the agent, replaces the compiler heap ci.fuzz would choose.
            docker run --name "${'$'}NAME" \
              --network none --cpus "${'$'}CPUS" --memory "${'$'}MEMORY" \
              -e TEAMCITY_VERSION -e HEPHAESTUS_JAVA_OPTS \
              -v "${'$'}PWD/bugs:/opt/hephaestus/bugs" \
              -v "${'$'}PWD/results:/results" \
              "${'$'}IMAGE" \
              "${'$'}HEPHAESTUS_ORACLE" --duration "${'$'}HEPHAESTUS_DURATION"
            rc=${'$'}?
            # Killing the docker client doesn't stop its container, hence the rm -f by name
            kill "${'$'}loop" 2>/dev/null
            wait "${'$'}loop" 2>/dev/null
            set -e

            # 0 = the time budget ran out, which is how a normal run ends
            if [ "${'$'}rc" -ne 0 ]; then
              echo "##teamcity[buildProblem description='Fuzzer exited with code ${'$'}rc']"
              exit "${'$'}rc"
            fi
            """.trimIndent()
        }
        script {
            name = "Collect results"
            executionMode = BuildStep.ExecutionMode.ALWAYS
            scriptContent = """
            #!/bin/bash
            # Cloud: step 3 "Collect results". Execute: Always, even if build stop command was issued.
            # Packs the findings, their programs (with dependency modules, sources only) and provenance into
            # results/final/; the build's artifact rules publish it with "Publish artifacts: Always".
            set -uo pipefail
            NAME="hephaestus-%teamcity.build.id%"
            IMAGE="hephaestus:%teamcity.build.id%"

            # Stopping the Fuzz container also terminates any checkpoint collector running via docker exec.
            # A cancelled build can leave the Fuzz container running: SIGTERM it first, so ci.fuzz shuts down
            # like Ctrl-C and still leaves a complete bugs/<name>/
            docker stop --time 120 "${'$'}NAME" >/dev/null 2>&1 || true

            if ! docker image inspect "${'$'}IMAGE" >/dev/null 2>&1; then
              echo "##teamcity[message text='No image was built, so there are no results to collect' status='WARNING']"
              exit 0
            fi

            mkdir -p bugs results/agent results/final && chmod a+rwx results/final
            # Exit code, start and end times: why the run stopped
            docker inspect "${'$'}NAME" > results/agent/container.json 2>/dev/null || rm -f results/agent/container.json

            # Two CPUs, or one on a one-CPU Docker host (docker refuses more than the host has)
            cpus=1
            [ "${'$'}(docker info --format '{{.NCPU}}' 2>/dev/null)" -ge 2 ] 2>/dev/null && cpus=2
            docker run --rm --network none --cpus "${'$'}cpus" --memory 4g --entrypoint python \
              -v "${'$'}PWD/bugs:/opt/hephaestus/bugs:ro" \
              -v "${'$'}PWD/results/agent:/agent:ro" \
              -v "${'$'}PWD/results/final:/out" \
              -e TEAMCITY_VERSION \
              "${'$'}IMAGE" -m ci.results --agent /agent --out /out --teamcity
            rc=${'$'}?

            if [ "${'$'}rc" -ne 0 ]; then
              # Never lose the finding records: publish the run's JSON files as they are
              echo "##teamcity[publishArtifacts 'bugs/*/*.json => hephaestus-results-json.zip']"
              echo "##teamcity[buildProblem description='Collecting results failed (exit ${'$'}rc); the JSON files were published as hephaestus-results-json.zip']"
              exit 1
            fi
            """.trimIndent()
        }
        script {
            name = "Cleanup"
            executionMode = BuildStep.ExecutionMode.ALWAYS
            scriptContent = """
            #!/bin/bash
            # Cloud: step 4 "Cleanup". Execute: Always, even if build stop command was issued.
            # Leaves results/final/ in place: the artifact rules publish it after this last step.
            IMAGE="hephaestus:%teamcity.build.id%"
            # The containers wrote bugs/ and results/ as `fuzzer`; give them back to the agent user so later
            # builds can delete them.
            if docker image inspect "${'$'}IMAGE" >/dev/null 2>&1; then
              for dir in bugs results; do
                [ -d "${'$'}dir" ] || continue
                docker run --rm --user 0 --network none --entrypoint chown \
                  -v "${'$'}PWD/${'$'}dir:/w" "${'$'}IMAGE" -R "${'$'}(id -u):${'$'}(id -g)" /w 2>/dev/null || true
              done
            fi
            # A container left behind keeps the image in use, so remove it before the image
            docker rm -f "hephaestus-%teamcity.build.id%" 2>/dev/null || true
            docker image rm "${'$'}IMAGE" 2>/dev/null || true
            """.trimIndent()
        }
    }

    failureConditions {
        executionTimeoutMin = 26 * 60
    }

    requirements {
        equals("docker.server.osType", "linux")
    }
})
