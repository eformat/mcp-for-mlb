#!/usr/bin/env python3
"""Write SpiceDB schema and seed initial relationships for MLB data agent."""

import os
import sys
from pathlib import Path

from authzed.api.v1 import (
    Client,
    ObjectReference,
    Relationship,
    RelationshipUpdate,
    SubjectReference,
    WriteRelationshipsRequest,
    WriteSchemaRequest,
)
from grpcutil import insecure_bearer_token_credentials

SPICEDB_ENDPOINT = os.environ.get("SPICEDB_ENDPOINT", "localhost:50051")
SPICEDB_TOKEN = os.environ.get("SPICEDB_TOKEN", "averysecretpresharedkey")

SCHEMA_PATH = Path(__file__).parent / "schema.zed"

DATASETS = [
    # High-level dataset names
    "batting", "pitching", "fielding", "teams", "parks",
    "postseason", "awards", "hall_of_fame", "salaries", "weather",
    # All actual table names
    "allstar_full", "appearances", "awards_managers", "awards_players",
    "awards_share_managers", "awards_share_players",
    "batting_post", "college_playing",
    "fielding_of", "fielding_of_split", "fielding_post",
    "home_games", "managers", "managers_half",
    "people", "pitching_post",
    "schools", "series_post",
    "teams_franchises", "teams_half",
    "weather_stations", "weather_daily",
    # Pitch-by-pitch tables
    "pitch_pitches", "pitch_atbats", "pitch_games",
    "pitch_player_names", "statcast_pitches", "pitch",
]

SEED_RELATIONSHIPS = []

# Organization
for rel in ["member", "admin"]:
    SEED_RELATIONSHIPS.append(("organization", "mlb", rel, "user", "admin"))
SEED_RELATIONSHIPS.append(("organization", "mlb", "member", "user", "viewer"))

# admin user: analyst on all datasets
for ds in DATASETS:
    SEED_RELATIONSHIPS.append(("dataset", ds, "analyst", "user", "admin"))

# viewer user: viewer on all datasets
for ds in DATASETS:
    SEED_RELATIONSHIPS.append(("dataset", ds, "viewer", "user", "viewer"))

# All datasets owned by mlb org
for ds in DATASETS:
    SEED_RELATIONSHIPS.append(("dataset", ds, "owner", "organization", "mlb"))

# Org admins are dataset admins
for ds in DATASETS:
    SEED_RELATIONSHIPS.append(("dataset", ds, "admin", "organization", "mlb#admin"))


def main():
    client = Client(
        SPICEDB_ENDPOINT,
        insecure_bearer_token_credentials(SPICEDB_TOKEN),
    )

    schema = SCHEMA_PATH.read_text()
    print(f"Writing schema from {SCHEMA_PATH}...")
    client.WriteSchema(WriteSchemaRequest(schema=schema))
    print("Schema written.")

    updates = []
    for res_type, res_id, relation, sub_type, sub_id in SEED_RELATIONSHIPS:
        sub_relation = ""
        if "#" in sub_id:
            sub_id, sub_relation = sub_id.split("#", 1)

        subject = SubjectReference(
            object=ObjectReference(object_type=sub_type, object_id=sub_id)
        )
        if sub_relation:
            subject.optional_relation = sub_relation

        updates.append(
            RelationshipUpdate(
                operation=RelationshipUpdate.Operation.OPERATION_TOUCH,
                relationship=Relationship(
                    resource=ObjectReference(object_type=res_type, object_id=res_id),
                    relation=relation,
                    subject=subject,
                ),
            )
        )

    print(f"Writing {len(updates)} relationships...")
    resp = client.WriteRelationships(WriteRelationshipsRequest(updates=updates))
    print(f"Relationships written. ZedToken: {resp.written_at.token}")


if __name__ == "__main__":
    main()
