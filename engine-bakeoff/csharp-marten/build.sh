#!/bin/sh
# Runs a command in the .NET 10 SDK container with engine-bakeoff/ mounted, so
# the tests can read shared/. A named volume caches NuGet packages. Set
# NETWORK to join a Compose network and reach its database, for example:
#   ./build.sh dotnet build
#   NETWORK=hivear-bakeoff-csharp_default ./build.sh dotnet test
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
case "$(uname -s)" in MINGW*|MSYS*) here="$(cygpath -w "$here")"; export MSYS_NO_PATHCONV=1 ;; esac
network=""
if [ -n "$NETWORK" ]; then network="--network $NETWORK"; fi
exec docker run --rm $network \
  -e DATABASE_HOST=db -e DATABASE_NAME=hivear -e DATABASE_USER=hivear \
  -e DATABASE_PASSWORD="${POSTGRES_PASSWORD:-local-evaluation-only}" -e WAX_APPEND="${WAX_APPEND:-}" -e WAX_HASH="${WAX_HASH:-}" \
  -v "$here:/work" -v wax-bakeoff-nuget:/root/.nuget/packages -w /work/csharp-marten \
  mcr.microsoft.com/dotnet/sdk@sha256:e70cdb7f80b0348f5cb85f19a8f670fca061f033d57eed12fa003d58b0e06317 "$@"
