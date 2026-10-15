# Design

The designs that are live, with their sources.

| Design | Preview | Live in |
|---|---|---|
| Landing page (editorial, five chapters) | [landing/editorial.png](landing/editorial.png) | `src/speckeeper/web/index.html`, served at the service's root |
| Architecture: cloud deployment | [architecture/cloud-deployment.png](architecture/cloud-deployment.png) | `docs/architecture.png`, the landing page and the README |
| Architecture: how a change travels | [architecture/how-a-change-travels.png](architecture/how-a-change-travels.png) | `docs/change-sequence.png`, the landing page and the README |

`src/` holds each design's source as a design-canvas file (`.dc.html`). They were made in a Claude design
canvas and need its runtime to open as designs; the PNGs are the readable versions. The landing page
preview was taken on 15 Oct 2026; the live page is the one to keep up to date. The two architecture
diagrams were updated on 16 Oct 2026 for the ADK pipelines, Agent Engine, Memory Bank, BigQuery and Cloud Trace.
