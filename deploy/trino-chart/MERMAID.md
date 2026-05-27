# Mermaid Diagrams

## Trino Architecture

```mermaid
flowchart TD
  UI[Trino Query UI] --> Coord[Trino Coordinator
+ Workers]
  Coord --> LLM[LLM - MaaS
Llama 4 Scout]
  Coord --> Nessie[Nessie Catalog]
  Coord --> MinIO[MinIO S3]
  Coord --> TPC[TPC-H / TPC-DS]
```
