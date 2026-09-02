# Design sources

The approved design the landing walkthrough was built from, kept in the repo
as provenance rather than as shipped code.

These files are **not** loaded by anything. They are Claude Design canvas
sources: they expect a `window.useComposition` / `CompositionStage` runtime
that does not exist in this project, and they will not run here. The
shippable output of that design is the vanilla, zero-build implementation one
directory up:

| Design source | Ships as |
|---|---|
| `aegis-hero.dc.html`, `aegis-hero.jsx` | `../hero.html`, `../hero.js`, `../hero.css` |
| `animations-v3.jsx`, `support.js`, `tweaks-panel.jsx` | the canvas runtime they were authored against |

Why they are here at all: the walkthrough makes specific claims — that the
filmed console is the real console's markup, that the blast radius is the
same reverse BFS the risk engine runs, that every sentence AEGIS "speaks" is
composed the way `governor/speech.py` composes it. These are the files that
show those claims were designed in rather than asserted afterwards.

They are served at `/static/design/*` because the whole `frontend/` directory
is mounted. That is harmless — they are inert text — but nothing links to
them and nothing should.
