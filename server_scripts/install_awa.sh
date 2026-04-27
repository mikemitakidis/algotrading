#!/bin/bash
# Install and activate the accept_write_access agent into the running Gateway JVM.
# Run once after Gateway is up. Agent persists until Gateway restarts.

set -e
SRC=/opt/algo-trader/server_scripts/accept_write_access.java
BUILD=/tmp/awa_build
GW_PID=$(pgrep -f "ibcalpha.ibc.IbcGateway" | head -1)

echo "Gateway PID: $GW_PID"
[ -z "$GW_PID" ] && echo "ERROR: Gateway not running" && exit 1

mkdir -p "$BUILD"
cp "$SRC" "$BUILD/"
javac --release 17 "$BUILD/accept_write_access.java" -d "$BUILD/"

cat > "$BUILD/MANIFEST.MF" << 'EOF'
Manifest-Version: 1.0
Agent-Class: accept_write_access
Can-Redefine-Classes: true
Can-Retransform-Classes: true

EOF

cd "$BUILD" && jar cmf MANIFEST.MF awa.jar accept_write_access.class
echo "JAR built"

jattach "$GW_PID" load instrument false "$BUILD/awa.jar"
echo "Agent loaded into Gateway JVM (PID $GW_PID)"
echo "Log: /tmp/accept_write_access.log"
echo ""
echo "Now run: python3 /opt/algo-trader/test_m11.py"
echo "The agent will auto-click Yes when the write-access dialog appears."
