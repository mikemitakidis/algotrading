#!/bin/bash
# Run this on the server to fix Gateway Read-Only mode
set -e
JAVA_SRC=/opt/algo-trader/server_scripts/fix_readonly_final.java
BUILD_DIR=/tmp/fix_final_build
GW_PID=$(pgrep -f "ibcalpha.ibc.IbcGateway" | head -1)

echo "Gateway PID: $GW_PID"
mkdir -p "$BUILD_DIR"
cp "$JAVA_SRC" "$BUILD_DIR/"
javac --release 17 "$BUILD_DIR/fix_readonly_final.java" -d "$BUILD_DIR/"

# Create JAR
cat > "$BUILD_DIR/MANIFEST.MF" << 'EOF'
Manifest-Version: 1.0
Agent-Class: fix_readonly_final
Can-Redefine-Classes: true
Can-Retransform-Classes: true

EOF
cd "$BUILD_DIR" && jar cmf MANIFEST.MF fix_final_agent.jar fix_readonly_final.class
echo "JAR built"

# Load into Gateway JVM
jattach "$GW_PID" load instrument false "$BUILD_DIR/fix_final_agent.jar"
echo "Agent loaded — watching log..."
sleep 12
cat /tmp/fix_final.log
