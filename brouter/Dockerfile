# BRouter for the Shfaim Shuttle Finder: a self-hosted, keyless pedestrian
# router, so the app stops depending on BRouter's public community server and
# its per-IP rate limit.
#
# The official image starts server.sh, which serves the BRouter HTTP API on
# the PORT env var (default 17777) and reads segment files from /segments4.
# Only the two 5x5 degree tiles covering the shuttle network are baked in
# (~58 MB): E30_N30 (Tel Aviv and the coast) and E35_N30 (Jerusalem and the
# east). They are the same weekly-built .rd5 files the public server uses,
# straight from brouter.de, so nothing needs a PBF-to-segment build here.

FROM ghcr.io/abrensch/brouter:v1.7.8

USER root
ADD https://brouter.de/brouter/segments4/E30_N30.rd5 /segments4/E30_N30.rd5
ADD https://brouter.de/brouter/segments4/E35_N30.rd5 /segments4/E35_N30.rd5
USER 1000

EXPOSE 17777