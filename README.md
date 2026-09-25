# byjwu

byjwu is an editing assistant for Final Cut Pro. You give it raw footage and music, or a timeline you've already started, and it hands back an FCPXML that opens in Final Cut as a new project, cut to organize a [@byjustinwu](https://www.youtube.com/@byjustinwu) video.

## How it works

```text
footage + music, or an FCPXML export
        ↓
Grok Bot room  →  conductor engine  ←  Jev (logic) + Opus 5.5 (taste)
        ↓
new FCPXML  →  Final Cut Pro
```

1. **Drop.** Footage, music, or an FCPXML export goes into a Grok Bot chat room or a watched folder.
2. **Analyse.** The engine, `conductor`, reads the clips or timeline: where the gaps, silences, and duplicate clips are, what's being said, and how loud the music is.
3. **Decide.** Clear problems, like a long gap or dead air, are cut by measured rules. Taste calls, like which shot opens or where a title goes, go to the taste model, Opus 5.5. A model can veto a rule's cut, but it can't invent one.
4. **Build.** The engine writes a new FCPXML. For an existing timeline, that's a copy with markers where it suggests changes, plus a version with the safe cuts applied. For raw footage, it assembles a full timeline: shots on the main storyline, cutaways above them, music that fades and dips under speech, and subtitles, titles, and background shapes.
5. **Review.** You open the file in Final Cut. Your original library and media aren't touched, and every change is marked so you can check it.

A room of bots talks you through each run. Cut Conductor runs it, and Pacing, Style, Type & Subs, and Colour each explain their part.

The look of an edit (shot lengths, fades, fonts, colours) comes from a style profile in `styles/`. The `byjustinwu` profile builds on the base one, and new profiles can do the same.

## Learn more

- [What changes in your FCPXML](docs/fcpxml-changes.md)
- [How the room works](docs/room-protocol.md)
- [Technical details](docs/technical.md)

built at a grok bot design build night in los angeles 09/22/26
