# syntax=docker/dockerfile:1.7
#
# Hephaestus continuous-fuzzing image (linux/amd64 only: Native ships x86_64 libcallbacks*.so).
# Lives at the repository root; TeamCity builds it from the VCS checkout of the selected branch.
#
# Build (CI passes the exact default-branch nightly; leave it empty to fall back to prerelease).
# Always build with --no-cache so the compiler store is re-resolved and re-verified, and with
# --target runtime because the last stage in this file is `test`.
#   docker build --no-cache --platform linux/amd64 --target runtime \
#     --build-arg KOTLIN_MASTER_VERSION=2.5.20-dev-3466 \
#     --build-arg KOTLIN_COMMIT=<kotlin sha> \
#     --build-arg HEPHAESTUS_COMMIT=$(git rev-parse HEAD) \
#     --build-arg HEPHAESTUS_BRANCH=hephaestus/ci-main \
#     -t hephaestus:2.5.20-dev-3466 .
#
# Run (fuzzing needs no network; the oracle is chosen at run time, not baked in). The entrypoint
# is `python -m ci.fuzz`: it stops by itself after --duration (exit 0), on `docker stop` (143) or
# at duration + --grace (124), and always leaves a complete bugs/<name>/.
#   mkdir -p bugs && chmod a+rwx bugs
#   docker run --rm --network none --cpus 16 --memory 32g \
#     -v "$PWD/bugs:/opt/hephaestus/bugs" hephaestus:2.5.20-dev-3466 \
#     backend-comparison --duration 23h
# Oracles: backend-comparison, inlining-native, inlining-js, inlining-wasm. Pass -e TEAMCITY_VERSION
# for TeamCity service messages and -e HEPHAESTUS_JAVA_OPTS to change the compiler heap (-Xmx8g).
#
# Inspect what an image contains:
#   docker inspect -f '{{json .Config.Labels}}' hephaestus:2.5.20-dev-3466
#   docker run --rm --entrypoint python hephaestus:2.5.20-dev-3466 \
#     src/tools/kotlin_compilers/manage.py list
#
# Tests:  docker build --no-cache --platform linux/amd64 --target test .

############################ base: Python 3.12 + full JDK 21 ############################
# Ubuntu 24.04 (noble): python3 is 3.12 (PEP 695 needed); Temurin JDK includes `jar`.
FROM --platform=linux/amd64 eclipse-temurin:21-jdk-noble AS base
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

############################ compilers: sync the store once ############################
FROM base AS compilers
ARG KOTLIN_MASTER_VERSION=""
# Only `master` (falling back to prerelease); Hephaestus uses no other compiler.
ENV KOTLIN_COMPILERS_HOME=/opt/kotlin-compilers \
    KOTLIN_COMPILERS_VERSIONS=/src/ci/versions.json
WORKDIR /src
# Only the stdlib-only manager + version list: code changes elsewhere don't re-download compilers.
COPY ci/versions.json ci/
COPY src/tools/kotlin_compilers/ src/tools/kotlin_compilers/
# Docker can't see new GitHub releases, so builds use --no-cache (see header).
RUN echo "master=${KOTLIN_MASTER_VERSION:-<fallback: prerelease>}" \
 && KOTLIN_MASTER_VERSION="${KOTLIN_MASTER_VERSION}" \
    python3 src/tools/kotlin_compilers/manage.py --host linux-x86_64 sync \
 && python3 src/tools/kotlin_compilers/manage.py list \
 && echo "Installed Kotlin master: $(basename "$(readlink -f "$KOTLIN_COMPILERS_HOME/master")")"
# Smoke test with networking disabled: proves Native stays first-stage (no LLVM download).
RUN --network=none python3 src/tools/kotlin_compilers/manage.py check

############################ runtime ############################
FROM base AS runtime
# Provenance only: recorded as labels, never read by Hephaestus at run time.
ARG KOTLIN_MASTER_VERSION=""
ARG KOTLIN_COMMIT=""
ARG HEPHAESTUS_COMMIT=""
ARG HEPHAESTUS_BRANCH=""
LABEL org.opencontainers.image.title="hephaestus" \
      org.opencontainers.image.source="https://github.com/AnzhelaSukhanova/hephaestus" \
      org.opencontainers.image.revision="${HEPHAESTUS_COMMIT}" \
      org.jetbrains.hephaestus.branch="${HEPHAESTUS_BRANCH}" \
      org.jetbrains.kotlin.master-version="${KOTLIN_MASTER_VERSION}" \
      org.jetbrains.kotlin.commit="${KOTLIN_COMMIT}"
ENV KOTLIN_COMPILERS_HOME=/opt/kotlin-compilers \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/hephaestus/.venv/bin:$PATH

# Compiler store is read-only at runtime (symlinks are preserved by COPY).
COPY --from=compilers /opt/kotlin-compilers /opt/kotlin-compilers

# noble already has user `ubuntu` (uid 1000), so let useradd pick the uid.
RUN useradd --create-home --shell /bin/bash fuzzer

WORKDIR /opt/hephaestus
# Hephaestus writes bugs/, temp/ and its log file in the working directory.
RUN chown fuzzer:fuzzer /opt/hephaestus
# Dependency layer first. Hephaestus runs from these sources, so only its dependencies
# (pyproject.toml and requirements.txt) go into the venv, not the package itself.
COPY pyproject.toml requirements.txt ./
RUN python3 -m venv .venv \
 && .venv/bin/python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements-project.txt \
 && .venv/bin/pip install --no-cache-dir -r /tmp/requirements-project.txt -r requirements.txt \
 && rm /tmp/requirements-project.txt
COPY --chown=fuzzer:fuzzer . .

USER fuzzer
# Writable HOME: Hephaestus' Native call writes $HOME/.konan (no -Xkonan-data-dir).
ENV HOME=/home/fuzzer
# tini forwards `docker stop`'s SIGTERM to ci.fuzz and reaps the shell=True compiler subprocesses.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "ci.fuzz"]
CMD ["backend-comparison"]

############################ test ############################
FROM runtime AS test
USER root
RUN .venv/bin/python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']['dev']))" > /tmp/requirements-dev.txt \
 && .venv/bin/pip install --no-cache-dir -r /tmp/requirements-dev.txt \
 && rm /tmp/requirements-dev.txt
USER fuzzer
# real_kotlin tests use the baked store; the rest use the fake master from conftest.
RUN python -m pytest -q
