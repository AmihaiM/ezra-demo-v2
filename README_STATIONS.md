EZRA station logic fix

Flow per sentence:
1. Station 1: listen/read the sentence.
2. Station 2: practice/mastery. If the student failed before passing, Bloom repetitions are required here only.
3. Station 3: cloze validation. The full English sentence is hidden; only the blanked sentence is shown.
4. Final exam: continuous assessment without mid-feedback; results are written to Google Sheets with mastery_status.

Notes:
- The teacher max_attempts setting is still a hard cap across practice + mastery + cloze for the current sentence.
- The cloze station no longer appears together with the Bloom mastery station.
- In cloze, the replay/reveal controls are hidden to avoid leaking the full English sentence.
