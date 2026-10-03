# byjwu

my assistant editor on Final Cut Pro. it takes care of single tasks, such as tightening a section, beat-matching b-roll, adding subtitles in an existing style, ducking music, and colour notes. capable of editing an entire video from end to end, from raw footage and music to a finished FCPXML format. personally built to enhance my [@byjustinwu](https://www.youtube.com/@byjustinwu) production workflow.

## how it works

```text
footage + music, or an FCPXML export
        ↓
Grok Bot room  →  conductor engine  ←  Jev (logic) + Opus 5.5 (yurr)
        ↓
new FCPXML  →  Final Cut Pro
```

1. **drop.** Footage, music, or an FCPXML export goes into a Grok Bot chat room or a watched folder.
2. **analyse.** The engine, `conductor`, reads the clips or timeline: where the gaps, silences, and duplicate clips are, what's being said, and how loud the music is.
3. **decide.** Clear problems, like a long gap or dead air, are cut by measured rules. Taste calls, like which shot opens or where a title goes, go to the taste model, Opus 5.5. A model can veto a rule's cut, but it can't invent one.
4. **build.** The engine writes a new FCPXML. For an existing timeline, that's a copy with markers where it suggests changes, plus a version with the safe cuts applied. For raw footage, it assembles a full timeline: shots on the main storyline, cutaways above them, music that fades and dips under speech, and subtitles, titles, and background shapes.
5. **review.** You open the file in Final Cut. Your original library and media aren't touched, and every change is marked so you can check it.

a room of bots talks you through each run. Cut Conductor runs it, and Pacing, Style, Type & Subs, and Colour each explain their part.

the look of an edit (shot lengths, fades, fonts, colours) comes from a style profile in `styles/`. The `byjustinwu` profile builds on the base one, and new editors/artists/creative people can do the same.

## learn more

- [What changes in your FCPXML](docs/fcpxml-changes.md)
- [How the room works](docs/room-protocol.md)
- [Technical details](docs/technical.md)

built at a grok bot design build night in los angeles 09/22/26 + iterated on random evenings in hawthorne, ca
