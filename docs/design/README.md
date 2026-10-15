# Design explorations

Four directions were drawn for the landing page and for the architecture diagram before picking one of
each. The chosen ones are live: the page at the service's root (`src/speckeeper/web/index.html`) and the
diagrams in `docs/architecture.png` and `docs/change-sequence.png`.

## Landing page

| Option | Preview | Notes |
|---|---|---|
| A · Product docs | [landing/A-product-docs.png](landing/A-product-docs.png) | Light, a live status card in the hero, steps as cards |
| B · Told as a thread | [landing/B-told-as-a-thread.png](landing/B-told-as-a-thread.png) | Dark; the hero replays the Slack thread, then "Your turn" |
| **C · Editorial (chosen)** | [landing/C-editorial-chosen.png](landing/C-editorial-chosen.png) | Black and white, five chapters: the problem, try it, what's new, under the hood, trust |
| D · Judge's scorecard | [landing/D-judges-scorecard.png](landing/D-judges-scorecard.png) | Organised around the four judging criteria and their weights |

## Architecture diagram

| Option | Preview | Notes |
|---|---|---|
| **A · Cloud deployment (chosen)** | [architecture/A-cloud-deployment-chosen.png](architecture/A-cloud-deployment-chosen.png) | External systems, the Cloud Run service, Google Cloud services, outputs |
| B · Layered stack | [architecture/B-layered-stack.png](architecture/B-layered-stack.png) | Where work happens, interfaces, core, Google Cloud |
| **C · How a change travels (chosen)** | [architecture/C-how-a-change-travels-chosen.png](architecture/C-how-a-change-travels-chosen.png) | A sequence diagram: 14 steps from a Slack message to a stale test |
| D · Blueprint hub | [architecture/D-blueprint-hub.png](architecture/D-blueprint-hub.png) | Hub and spoke inside the project boundary |

## Sources

`src/` holds each option's source as a design-canvas file (`.dc.html`). They were made in a Claude design
canvas and need its runtime to open as designs; the PNGs above are the readable versions. Text in them
was current on 15 Oct 2026; the live page is the one to keep up to date.
