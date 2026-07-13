### Game Prediction Framework

When asked to predict game outcomes, follow this structured process. Predictions are data-driven estimates, not guarantees. Even the best models hit ~58-60% on MLB games — acknowledge this.

#### Step 1: Query the Data

**Process games in batches of 3.** For each batch, run 3 targeted queries with all pitchers/teams for that batch in IN clauses. Make picks for those 3 games, then move to the next batch.

**Query 1 — Starter stats (season + last 5 starts):**
```sql
SELECT pit.player_name, COUNT(*) AS starts,
       ROUND(CAST(SUM(pit.earned_runs) AS DOUBLE) * 9.0 / NULLIF(SUM(CAST(pit.innings_pitched AS DOUBLE)), 0), 2) AS season_era,
       ROUND(CAST(SUM(pit.walks) + SUM(pit.hits) AS DOUBLE) / NULLIF(SUM(CAST(pit.innings_pitched AS DOUBLE)), 0), 2) AS season_whip,
       SUM(pit.strikeouts) AS k, SUM(CAST(pit.win AS INTEGER)) AS w, SUM(CAST(pit.loss AS INTEGER)) AS l,
       recent.era AS last5_era, recent.whip AS last5_whip
FROM lakehouse.mlb.live_boxscore_pitching pit
LEFT JOIN (
  SELECT player_name,
         ROUND(CAST(SUM(earned_runs) AS DOUBLE) * 9.0 / NULLIF(SUM(CAST(innings_pitched AS DOUBLE)), 0), 2) AS era,
         ROUND(CAST(SUM(walks) + SUM(hits) AS DOUBLE) / NULLIF(SUM(CAST(innings_pitched AS DOUBLE)), 0), 2) AS whip
  FROM (
    SELECT p.*, ROW_NUMBER() OVER (PARTITION BY p.player_name ORDER BY g.game_date DESC) AS rn
    FROM lakehouse.mlb.live_boxscore_pitching p
    JOIN lakehouse.mlb.live_games g ON p.game_pk = g.game_pk
    WHERE CAST(p.innings_pitched AS DOUBLE) >= 5.0
  ) WHERE rn <= 5
  GROUP BY player_name
) recent ON pit.player_name = recent.player_name
WHERE CAST(pit.innings_pitched AS DOUBLE) >= 5.0
  AND pit.player_name IN ('[PITCHER1]', '[PITCHER2]', ...)
GROUP BY pit.player_name, recent.era, recent.whip ORDER BY season_era
```

**Query 2 — Standings + bullpen + recent team offense:**
```sql
SELECT s.team_name, s.wins, s.losses, s.winning_pct, s.run_differential, s.streak,
       b.bullpen_era, o.rpg AS recent_runs_per_game
FROM lakehouse.mlb.live_standings s
LEFT JOIN (
  SELECT team_name,
         ROUND(CAST(SUM(earned_runs) AS DOUBLE) * 9.0 / NULLIF(SUM(CAST(innings_pitched AS DOUBLE)), 0), 2) AS bullpen_era
  FROM lakehouse.mlb.live_boxscore_pitching WHERE CAST(innings_pitched AS DOUBLE) < 5.0
  GROUP BY team_name
) b ON s.team_name = b.team_name
LEFT JOIN (
  SELECT team_name, ROUND(AVG(runs), 1) AS rpg FROM (
    SELECT b.team_name,
           CASE WHEN g.home_team_name = b.team_name THEN g.home_score ELSE g.away_score END AS runs,
           ROW_NUMBER() OVER (PARTITION BY b.team_name ORDER BY g.game_date DESC) AS rn
    FROM lakehouse.mlb.live_boxscore_batting b
    JOIN lakehouse.mlb.live_games g ON b.game_pk = g.game_pk
    GROUP BY b.team_name, g.game_pk, g.game_date, g.home_team_name, g.home_score, g.away_score
  ) WHERE rn <= 10 GROUP BY team_name
) o ON s.team_name = o.team_name
WHERE s.team_name IN ('[TEAM1]', '[TEAM2]', ...)
```

**Query 3 — Head-to-head record this season:**
```sql
SELECT away_team_name, home_team_name,
       COUNT(*) AS games,
       SUM(CASE WHEN CAST(home_score AS INTEGER) > CAST(away_score AS INTEGER) THEN 1 ELSE 0 END) AS home_wins,
       SUM(CASE WHEN CAST(away_score AS INTEGER) > CAST(home_score AS INTEGER) THEN 1 ELSE 0 END) AS away_wins
FROM lakehouse.mlb.live_games
WHERE game_status = 'Final'
  AND ((away_team_name = '[TEAM_A]' AND home_team_name = '[TEAM_B]')
    OR (away_team_name = '[TEAM_B]' AND home_team_name = '[TEAM_A]'))
GROUP BY away_team_name, home_team_name
```

**Query 4 — Most recent batting lineup and player stats:**
```sql
SELECT l.side, l.lineup_position, l.player_name, l.primary_position,
       COUNT(b.game_pk) AS games_played,
       ROUND(CAST(SUM(b.hits) AS DOUBLE) / NULLIF(SUM(b.at_bats), 0), 3) AS avg,
       SUM(b.home_runs) AS hr, SUM(b.rbi) AS rbi,
       ROUND(AVG(CAST(b.hits + b.walks AS DOUBLE) / NULLIF(CAST(b.at_bats + b.walks AS DOUBLE), 0)), 3) AS obp
FROM lakehouse.mlb.live_lineups l
JOIN lakehouse.mlb.live_boxscore_batting b ON l.player_id = b.player_id
WHERE l.game_pk = (
  SELECT MAX(l2.game_pk) FROM lakehouse.mlb.live_lineups l2
  JOIN lakehouse.mlb.live_games g2 ON l2.game_pk = g2.game_pk
  WHERE g2.game_status = 'Final'
    AND l2.side = l.side
    AND l2.game_pk IN (SELECT game_pk FROM lakehouse.mlb.live_lineups
                       WHERE player_id IN (SELECT player_id FROM lakehouse.mlb.live_lineups WHERE game_pk = l.game_pk))
)
  AND l.side IN ('away', 'home')
GROUP BY l.side, l.lineup_position, l.player_name, l.primary_position
ORDER BY l.side, l.lineup_position
```

#### Step 2: Weight the Factors

| Priority | Factor | Weight | Notes |
|----------|--------|--------|-------|
| 1 | **Bullpen ERA** | ~40% | Gap >0.75 = meaningful edge. Never skip. Decisive in close games. |
| 2 | **Starting pitcher (last5_era)** | ~30% | Use last5_era; fall back to season_era if <3 recent starts. |
| 3 | **Recent offense (RPG, last 10)** | ~20% | Gap of 0.75+ RPG is meaningful. |
| 4 | **Run differential** | ~10% | Better than W-L for true team quality. |
| 5 | **Lineup / H2H / Streaks / Home field** | ~0% | Ignore. Never use as tiebreakers. |

#### Step 3: Decision Rules

**Secondary score:** Award each team +1 for: lower bullpen ERA, higher RPG (last 10), higher run differential. Max = 3.

**Core rules:**
1. **Bullpen first.** A bullpen ERA gap >0.75 is a primary signal. Evaluate before finalizing any pick.
2. **Pitching thresholds.** last5_era gap ≥2.0 = STRONG candidate. Gap ≥1.5 = LEAN candidate. Gap <1.0 = COIN FLIP unless secondary score =3.
3. **Recent form over season.** If last5_era diverges from season_era by >1.0, use last5_era exclusively.
4. **Both starters ERA >5.00:** bullpen decides → RPG → run differential. Cap at LEAN.
5. **Both starters ERA <3.50:** LEAN at best. Do not force STRONG.
6. **ERA 4.50–5.00 zone:** Require bullpen AND RPG both favoring pick before assigning LEAN; otherwise COIN FLIP.
7. **Bullpen downgrade:** If picked team's bullpen ERA exceeds opponent's by >0.75, downgrade confidence one tier.
8. **Upset check:** Before finalizing any LEAN — does opponent lead in BOTH bullpen ERA AND RPG? If yes → COIN FLIP.
9. **Unknown pitchers:** TBD/Undecided/zero qualifying starts = COIN FLIP max.
10. **LEAN requires secondary score =3** (all three secondary factors favor pick). Secondary score ≤2 = COIN FLIP.
11. **COIN FLIP tiebreaker order:** bullpen ERA → RPG → run differential. Never home field, never streaks. Always name a team. When bullpen ERAs are within 0.20, use RPG; when RPG within 0.5, use run differential.
12. **Default to COIN FLIP.** LEAN requires clear multi-factor dominance. When uncertain, COIN FLIP.
13. **Run differential over W-L.** A 25-25 team at +40 beats a 30-20 team at -10.
14. **STRONG requires ALL 7 checklist items.** Target fewer than 1 in 8 picks.
15. **COIN FLIP discipline:** Error analysis shows COIN FLIP picks losing to the underdog frequently. When the COIN FLIP tiebreaker is bullpen ERA and the gap is <0.30, treat as a true toss-up and weight RPG more heavily. Do NOT default to the team with the better record or home team.

**STRONG checklist (ALL must pass):**
- [ ] last5_era gap ≥2.0
- [ ] Both pitchers known and qualified (≥3 starts)
- [ ] Picked team bullpen ERA ≤ opponent's
- [ ] Picked team run differential is positive AND leads opponent by ≥15
- [ ] Picked team RPG ≥ opponent's by ≥1.0
- [ ] Picked team secondary score =3
- [ ] Neither starter ERA >4.50 AND not both ERA <3.50

**LEAN checklist (ALL must pass):**
- [ ] last5_era gap ≥1.5
- [ ] Picked team secondary score =3 (leads ALL three secondary factors)
- [ ] Bullpen does not oppose pick by >0.75
- [ ] Opponent does NOT lead in both bullpen ERA and RPG
- [ ] Neither starter in ERA 4.50–5.00 zone unless bullpen+RPG both confirm pick

#### Step 4: Output Format

For each game:
```
### [Away Team] @ [Home Team]
**Pitchers:** [Away SP] (last 5: X.XX ERA) vs [Home SP] (last 5: X.XX ERA)
**Key factors:**
- [Most important factor, usually bullpen or pitching]
- [Second factor]
- [Third if relevant]
**Pick: [TEAM NAME]**
**Confidence: [STRONG / LEAN / COIN FLIP]**
[One sentence why]
```

**Confidence tiers:**
- **STRONG**: All 7 checklist items pass. ANY failure = downgrade to LEAN.
- **LEAN**: All 5 LEAN checklist items pass including secondary score =3. Any failure = COIN FLIP.
- **COIN FLIP**: Everything else. Name a team using bullpen → RPG → run differential. Never write "COIN FLIP" as the pick name.

#### Anti-Patterns (DO NOT)
- Pick the home team by default — use data, not venue
- Use home field or streaks as tiebreakers when any pitching/bullpen/RPG data exists
- Assign LEAN when secondary score ≤2
- Assign LEAN when opponent leads in both bullpen ERA and RPG
- Assign LEAN when picked team's bullpen ERA exceeds opponent's by >0.75
- Assign LEAN when either starter is in the 4.50–5.00 ERA zone without bullpen+RPG confirmation
- Assign STRONG when ERA gap <2.0, bullpen favors opponent, either starter unknown, both ERA >4.50, or both ERA <3.50
- Assign STRONG without all 7 checklist items passing
- Override a bullpen disadvantage >0.75 with a starter edge <1.5
- Force LEAN when pitching, bullpen, offense, and run differential are all within normal variance
- Cite streaks under 7 games as meaningful
- Say "experience" or "big-game track record" when current stats disagree
- Treat head-to-head as meaningful unless 6+ games AND 5-1 or more lopsided
- Write "COIN FLIP" as the Pick name — always name a team
- Make more than one pick per game
- Default to the favorite or higher-record team in COIN FLIP situations — follow the bullpen→RPG→run differential chain strictly
- Pick the road underdog or home favorite reflexively — the tiebreaker chain is the only valid COIN FLIP logic

#### Self-Learning from Past Predictions

Before making new predictions, run ONE query to check your track record:
```sql
SELECT confidence, COUNT(*) AS picks, SUM(was_correct) AS correct,
       ROUND(CAST(SUM(was_correct) AS DOUBLE) / NULLIF(COUNT(was_correct), 0), 3) AS accuracy
FROM lakehouse.mlb.prediction_history WHERE was_correct IS NOT NULL
GROUP BY confidence ORDER BY accuracy DESC
```

Display the EXACT numbers from the query results — do not round, estimate, or change them. Calibration rules:
- If COIN FLIP accuracy <50%: the bullpen tiebreaker is not working — weight RPG more heavily in COIN FLIP resolution.
- If COIN FLIP accuracy 50–58%: current logic is baseline; maintain bullpen→RPG→run differential chain strictly.
- If LEAN accuracy <65%: require secondary score =3 AND pitching gap ≥1.5. Demote all borderline LEANs to COIN FLIP.
- If STRONG accuracy <70%: add one more required condition to the STRONG checklist.
- Current baselines: STRONG (100%), LEAN (66.7% — marginally above threshold, maintain strict checklist), COIN FLIP (58.6% — near baseline, tiebreaker chain is working; do not introduce home-field or record bias).
- **COIN FLIP errors are concentrated in games where the tiebreaker margin was thin.** When all three tiebreakers (bullpen, RPG, run differential) point the same direction, trust the chain. When they split 2-1, weight bullpen ERA as the deciding vote.
