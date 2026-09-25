# byjwu

byjwu edits [@byjustinwu](https://www.youtube.com/@byjustinwu) videos: raw footage and music go in, and a Final Cut Pro FCPXML comes out.

## How it works

```text
footage, music, or an FCPXML
        ↓
Grok Bot room  →  engine (conductor)  ←  Jev + Claude Opus 5.5
        ↓
FCPXML  →  Final Cut Pro
```

- **In:** footage, music, or an FCPXML, dropped in the Grok Bot room or `~/Desktop/byjwu-in`.
- **Room:** the Grok bots coordinate: Cut Conductor, Pacing, Style, Type & Subs, and Colour.
- **Decisions:** Jev makes the logical calls. Claude Opus 5.5 makes the taste calls.
- **Engine:** `conductor` builds the edit, then marks it or applies the safe cuts.
- **Out:** an FCPXML in `~/Desktop/byjwu-out`, opened in Final Cut.

Details are in [docs/](docs/technical.md).

built at a grok bot design build night in los angeles 09/22/26
