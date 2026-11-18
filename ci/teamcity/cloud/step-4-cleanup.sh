#!/bin/bash
# Cloud: step 4 "Cleanup". Execute: Always, even if build stop command was issued.
# Leaves results/final/ in place: the artifact rules publish it after this last step.
IMAGE="hephaestus:%teamcity.build.id%"
# The containers wrote bugs/ and results/ as `fuzzer`; give them back to the agent user so later
# builds can delete them.
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  for dir in bugs results; do
    [ -d "$dir" ] || continue
    docker run --rm --user 0 --network none --entrypoint chown \
      -v "$PWD/$dir:/w" "$IMAGE" -R "$(id -u):$(id -g)" /w 2>/dev/null || true
  done
fi
# A container left behind keeps the image in use, so remove it before the image
docker rm -f "hephaestus-%teamcity.build.id%" 2>/dev/null || true
docker image rm "$IMAGE" 2>/dev/null || true
