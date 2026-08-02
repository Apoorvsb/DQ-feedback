"""Run 3 — build report.html from everything stored so far."""
import engine

path = engine.build_report()
vs = engine.versions()
fb = engine.all_feedback()

print(f"wrote {path}")
print(f"  {len([v for v in vs if not v['is_control']])} version(s) "
      f"+ {len([v for v in vs if v['is_control']])} control")
print(f"  {len([f for f in fb if f['status']=='accepted'])} accepted, "
      f"{len([f for f in fb if f['status']=='rejected'])} rejected")
print(f"\nopen it:  xdg-open {path}")
