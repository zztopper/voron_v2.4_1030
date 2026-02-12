#!/bin/bash
# update_spoolman_location.sh
# Updates a spool's location in Spoolman when assigned to a BoxTurtle lane.
# Usage: update_spoolman_location.sh <spool_id> <lane_name>
#   lane_name: lane1..lane4  -> "BoxTurtle Lane 1".."BoxTurtle Lane 4"
#              shelf         -> "Shelf"
#              any other     -> passed as-is

SPOOLMAN_URL="https://spoolman.r2bnj.ru"

SPOOL_ID="$1"
LANE="$2"

if [ -z "$SPOOL_ID" ] || [ -z "$LANE" ]; then
    echo "Usage: $0 <spool_id> <lane_name>"
    exit 1
fi

# Map lane names to Spoolman location strings
case "$LANE" in
    lane1) LOCATION="BoxTurtle Lane 1" ;;
    lane2) LOCATION="BoxTurtle Lane 2" ;;
    lane3) LOCATION="BoxTurtle Lane 3" ;;
    lane4) LOCATION="BoxTurtle Lane 4" ;;
    shelf) LOCATION="Shelf" ;;
    *)     LOCATION="$LANE" ;;
esac

curl -sk -X PATCH "${SPOOLMAN_URL}/api/v1/spool/${SPOOL_ID}" \
    -H "Content-Type: application/json" \
    -d "{\"location\": \"${LOCATION}\"}" \
    -o /dev/null -w "%{http_code}"
