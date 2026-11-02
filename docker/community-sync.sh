#!/bin/sh
# TERM the parent only. Never interrupt the active importer.
stopping=0
sleeper=
interrupted=0
trap 'stopping=1; interrupted=1; if [ -n "$sleeper" ]; then kill -TERM "$sleeper" 2>/dev/null || :; fi' TERM INT
while [ "$stopping" -eq 0 ]; do
    arsenal sync-online </dev/null &
    child=$!
    result=0
    while :; do
        interrupted=0
        wait "$child"
        result=$?
        [ "$interrupted" -eq 1 ] || break
    done
    if [ "$result" -eq 0 ]; then
        date -u +%FT%TZ > /tmp/sync-ok
        rm -f /tmp/sync-failed
    else
        rm -f /tmp/sync-ok
        date -u +%FT%TZ > /tmp/sync-failed
    fi
    [ "$stopping" -eq 0 ] || break
    sleep 1200 &
    sleeper=$!
    [ "$stopping" -eq 0 ] || kill -TERM "$sleeper" 2>/dev/null || :
    wait "$sleeper" || :
    wait "$sleeper" 2>/dev/null || :
    sleeper=
done
exit 0
