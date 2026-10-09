# The C# side of the engine bake-off. Build from engine-bakeoff/, so that the
# tests can read the shared hash vectors:
#   docker build -f csharp-wax/Dockerfile .
# Base images are pinned by digest so that a rebuild starts from the same bytes.

# mcr.microsoft.com/dotnet/sdk:10.0
FROM mcr.microsoft.com/dotnet/sdk@sha256:e70cdb7f80b0348f5cb85f19a8f670fca061f033d57eed12fa003d58b0e06317 AS build
WORKDIR /work/csharp-wax
# Restore first, from the project files alone, so that a code change does not
# download the packages again.
COPY csharp-wax/Directory.Build.props csharp-wax/Directory.Packages.props csharp-wax/Bakeoff.slnx ./
COPY csharp-wax/src/Wax/Wax.csproj src/Wax/
COPY csharp-wax/src/HiveAR/HiveAR.csproj src/HiveAR/
COPY csharp-wax/src/Bakeoff.Host/Bakeoff.Host.csproj src/Bakeoff.Host/
COPY csharp-wax/tests/Bakeoff.Tests/Bakeoff.Tests.csproj tests/Bakeoff.Tests/
RUN dotnet restore Bakeoff.slnx
COPY shared/ /work/shared/
COPY csharp-wax/ ./
RUN dotnet build Bakeoff.slnx -c Release --no-restore -nologo \
 && dotnet publish src/Bakeoff.Host -c Release --no-build -o /app

# The tests, run against a database by the Compose tools service.
FROM build AS test
CMD ["dotnet", "test", "Bakeoff.slnx", "-c", "Release", "--no-build", "--logger", "console;verbosity=normal"]

# mcr.microsoft.com/dotnet/runtime:10.0
FROM mcr.microsoft.com/dotnet/runtime@sha256:b89586dc17781f25531909993658aa8161205ae38b8cec8847df4a8221a403d5 AS runtime
WORKDIR /app
COPY --from=build /app ./
# The image's own unprivileged user.
USER app
ENTRYPOINT ["dotnet", "Bakeoff.Host.dll"]
# The engine's long-running process keeps the read models current (WAX-0001).
# Commands are dispatched in-process by whatever drives the engine, such as
# the tests or the bake-off harness.
CMD ["run-projections"]
