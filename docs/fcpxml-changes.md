# FCPXML changes

## Retakes and word gaps

Two cut types can land on a timeline when a clip has word timings.

**Retake.** An earlier take, or a false start, is lifted. The marker on the
shadow timeline is titled "Retake". The note reads "Earlier take removed.
The last complete take was kept." The kept take is the last complete reading
of the line, unless the taste call had to choose between complete takes that
scored too close to separate and named a different one.

**Retake kept.** When that cut is vetoed, nothing is lifted. The marker is
titled "Retake kept". The note starts "Both takes kept." and then gives the
reason, in plain words.

**Pause shortened.** Dead air between half a second and 1.25 seconds is not
removed. The cut leaves the style-profile pause (0.18 seconds unless the
profile says otherwise). A bare gap of 1.25 seconds or more is still removed,
which is the silence cut the mechanical pass already makes. A cross dissolve
is written only when the removed stretch is at least one second and the
style profile allows dissolves. Anything shorter stays a hard cut.

**Filler.** "um" and "uh" are cut only when there is at least 80 milliseconds
of silence on one side. "like", "you know", and "so" are not cut until the
taste call approves them. Until then they are only a proposal.
