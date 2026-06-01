#!/usr/bin/env bash
#
# Check MLB game statuses for a given date.
#
# Usage:
#   ./scripts/check-games.sh              # today
#   ./scripts/check-games.sh 2026-05-28   # specific date
#
set -euo pipefail

DATE="${1:-$(date +%Y-%m-%d)}"

curl -s "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=${DATE}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
dates = data.get('dates', [])
if not dates:
    print(f'No games scheduled for ${DATE}')
    sys.exit(0)
final, live, pre = 0, 0, 0
for d in dates:
    for g in d.get('games', []):
        abstract = g['status']['abstractGameState']
        status = g['status']['detailedState']
        away = g['teams']['away']['team']['name']
        home = g['teams']['home']['team']['name']
        score_a = g['teams']['away'].get('score', '-')
        score_h = g['teams']['home'].get('score', '-')
        if abstract == 'Final':
            final += 1
            print(f'  \033[32mFinal\033[0m       {away:25s} {score_a:>3} @ {home:25s} {score_h:>3}')
        elif abstract == 'Live':
            live += 1
            inning = g['linescore']['currentInningOrdinal'] if 'linescore' in g else '?'
            print(f'  \033[33mLive ({inning})\033[0m  {away:25s} {score_a:>3} @ {home:25s} {score_h:>3}')
        else:
            pre += 1
            print(f'  \033[90mPre-Game\033[0m    {away:25s}     @ {home:25s}')
print(f'\n  Final: {final}  Live: {live}  Pre-Game: {pre}  Total: {final+live+pre}')
"
