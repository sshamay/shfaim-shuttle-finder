# BRouter for the Shfaim Shuttle Finder: a self-hosted, keyless pedestrian
# router, so the app stops depending on BRouter's public community server and
# its per-IP rate limit.
#
# The official image runs /bin/server.sh, which execs btools.server.RouteServer
# with hardcoded settings (port 17777, 1 thread, its own JAVA_OPTS line) and
# reads segments from /segments4 and lookups from /profiles2/lookups.dat.
# The two 5x5 degree tiles covering the shuttle network are baked in
# (~58 MB): E30_N30 (Tel Aviv and the coast) and E35_N30 (Jerusalem and the
# east). They are the same weekly-built .rd5 files the public server uses,
# straight from brouter.de, so nothing needs a PBF-to-segment build here.
#
# The image's pinned lookups.dat is older than the current brouter.de tiles:
# BRouter refuses to load tiles unless the lookup versions agree ("lookup
# version mismatch (old rd5?) lookups.dat=10 E30_N30.rd5=11"), so lookups.dat
# from the BRouter source that built those tiles overwrites the stale copy.
# Bump brouter/lookups.dat together with the tiles the day their lookup
# version moves (the mismatch error prints both numbers).

FROM ghcr.io/abrensch/brouter:v1.7.8

USER root
# --chmod is required: ADD from a URL creates root-only 0600 files, and the
# target /segments4 directory comes out 0644, so the non-root server (USER
# 1000) can neither traverse the dir nor read the tiles ("Permission denied"
# / "datafile ... not found"). Fix both.
ADD --chmod=0644 https://brouter.de/brouter/segments4/E30_N30.rd5 /segments4/E30_N30.rd5
ADD --chmod=0644 https://brouter.de/brouter/segments4/E35_N30.rd5 /segments4/E35_N30.rd5
COPY brouter/lookups.dat /profiles2/lookups.dat
RUN chmod 0755 /segments4 && chown -R 1000:1000 /segments4 /profiles2/lookups.dat
USER 1000

EXPOSE 17777