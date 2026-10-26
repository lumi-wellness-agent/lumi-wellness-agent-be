# PR-05 audio attribution and processing

PR-05 returns all three fixed candidates to the client. The client selects one asset, with
`Miserere mei, Deus` marked as the default. All files are limited to 600 seconds of playback.

## Miserere mei, Deus

- Work: `Miserere mei, Deus`
- Composer: Gregorio Allegri
- Performance: Cantores Carmeli Linz
- Director and publisher: Michael Stenov
- Recording: live at Karmelitenkirche Linz, 2006
- Source: [IMSLP recording #577802](https://imslp.org/wiki/Miserere_(Allegri,_Gregorio)#Recordings)
- License: [Creative Commons Attribution-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-sa/4.0/)

The source recording is 10 minutes 44 seconds long. LUMI preserves the downloaded MP3 and
limits playback to the first 600 seconds for PR-05. This differs from the Tallis Scholars recording
used in E09 and is presented as an `ADAPTED_STIMULUS`, not as the original research stimulus.

SHA-256:
`A9733325D72DD56E07707100960BCBD9A83442ED0B94D3A7CDFA938400AF3757`

## Sonata for Two Pianos in D major, K.448

- Composer: Wolfgang Amadeus Mozart
- Performance: NoNameTheAnt (synthesized piano performance), 2023
- Source: [IMSLP recording #885399](https://imslp.org/wiki/Sonata_for_2_Pianos%2C_K.448_%28Mozart%2C_Wolfgang_Amadeus%29#Synthesized/MIDI)
- License: [Creative Commons Attribution-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-sa/4.0/)
- Processing: first 600 seconds re-encoded to 128 kbps MP3
- Evidence fidelity: `ADAPTED_STIMULUS` (same composition family as E08, different performance)

SHA-256:
`A394717A7D5A396207E113AEF9BE0AAAFC97ADE141ADC84D5A2968F233DFF0D2`

## The Four Seasons: Spring, RV 269

- Composer: Antonio Vivaldi
- Performance: John Harrison; Wichita State University Chamber Players; Robert Turizziani
- Recording: Wiedemann Hall, 2000
- Source: IMSLP recordings [#520727, #520736, and #520729](https://imslp.org/wiki/Violin_Concerto_in_E_major%2C_RV_269_%28Vivaldi%2C_Antonio%29#Recordings)
- License: [Creative Commons Attribution-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-sa/4.0/)
- Processing: three movements concatenated in score order; first 600 seconds re-encoded to 128 kbps MP3
- Evidence fidelity: `ADAPTED_STIMULUS` (related work from E08, different recording and presentation)

SHA-256:
`993BE445CB24A1B029DC84BF972291C4CD93FE1BEBE315F6973B3CF8FBD37736`

All three files passed an FFmpeg full-decode check. Final human listening review remains open in
`data/protocols/PR-05.json`; technical validation is not represented as completed clinical review.
