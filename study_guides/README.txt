Drop your study-guide .html files here (the ones you make in Claude).

Each guide becomes an exam on the dashboard's Study schedule, planned backward
from its date. The bot reads the guide two ways:

1. Best: include a metadata block near the top of the guide --
   <script type="application/study-guide+json">
   { "course": "CHMY 141", "unit": "Unit 4", "exam": "Exam 2",
     "exam_date": "2026-10-20",
     "sections": ["4.1 Empirical formulas", "4.2 Hydrates"] }
   </script>

2. If there's no block, the bot guesses from the title, an "Exam ... <Month>
   <day>" line, a "Covers ..." line, and numbered section headings.

Run `python morning_brief.py --check` to see what it read from each guide.
These files stay on your computer (git ignores them).
