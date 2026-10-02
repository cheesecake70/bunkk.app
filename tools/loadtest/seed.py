"""N verified students, each with a realistic ledger, timetable and term end.

usage: DATABASE_URL=sqlite:////tmp/load.db BUNKR_UPLOAD_DIR=/tmp/load_uploads \\
           BUNKR_CONFIG=config.DevConfig python tools/loadtest/seed.py 200

Never point this at a real database: it creates a College row and N accounts.
"""
import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [REPO, os.path.join(REPO, "tests")]
import random, time
from datetime import date, time as dtime, timedelta
from pathlib import Path

from flask_migrate import upgrade
from reportlab_stub import make_detailed_pdf

from app import create_app, db
from app import planning
from app.merge import ingest
from app.models import College, Settings, User

N = int(sys.argv[1]) if len(sys.argv) > 1 else 200
SUBJECTS = ["Computer NetworksT C2", "Data StructuresT C2", "Data Structures LaboratoryP C22",
            "Database Management SystemT C2", "Database Management System Lab C22",
            "Operations Research T Div- 2", "Statistics for Data ScienceT C1-C2",
            "Computational MathematicsT C2", "Universal Human ValuesT C2",
            "Universal Human Values TutorialU C2", "Design Thinking LaboratoryU C2",
            "Python Programming LaboratoryP C22", "Community Engagement ServiceU C2",
            "Innovative Product Development I Comp"]
SLOTS = [dtime(9, 0, 1), dtime(10, 0, 1), dtime(11, 0, 1), dtime(13, 0, 1), dtime(14, 0, 1)]

def rows_for(seed):
    rng = random.Random(seed)
    out = []
    start = date(2026, 7, 13)   # a Monday
    for week in range(9):
        for wd in range(5):
            day = start + timedelta(days=7 * week + wd)
            for i, slot in enumerate(SLOTS[:3]):
                subj = SUBJECTS[(wd * 3 + i) % len(SUBJECTS)]
                status = rng.choices(["P", "A", "NU"], [0.72, 0.13, 0.15])[0]
                end = dtime(slot.hour + 1, 0, 1)
                out.append((subj, day, slot, end, status))
    return out

app = create_app()
with app.app_context():
    upgrade(directory=os.path.join(REPO, "migrations"))
    college = College(name="SVKM"); db.session.add(college); db.session.commit()
    pdf_dir = Path(os.environ.get("BUNKR_LOAD_PDFS", os.path.join(REPO, "instance", "load_pdfs"))); pdf_dir.mkdir(exist_ok=True)
    t0 = time.time()
    for i in range(N):
        user = User(email=f"student{i}@example.com", username=f"student{i}", college_id=college.id)
        user.google_sub = f"dev-student{i}@example.com"; user.mark_verified()
        db.session.add(user); db.session.flush()
        db.session.add(Settings(user_id=user.id, subject_limit=70, overall_limit=75))
        db.session.commit()
        pdf = pdf_dir / f"student{i}.pdf"
        make_detailed_pdf(pdf, rows_for(i), period_start=date(2026, 7, 13), period_end=date(2026, 9, 11),
                          student_number=f"6000{i:07d}", roll_no=f"C{i:03d}")
        with app.test_request_context():
            result = ingest(user, pdf.read_bytes(), pdf.name)
            assert result.status == "merged", result.status
            slots = [c.slot for c in planning.inferred_candidates(user) if c.is_confident]
            planning.save_timetable(user, slots, source="manual")
            planning.set_semester_end(user, date(2026, 12, 15))
        if i % 50 == 49:
            print(f"seeded {i+1} students in {time.time()-t0:.0f}s", flush=True)
    print("users:", db.session.query(User).count())
