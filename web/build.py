"""Build the two pages the server serves: inline shared.css and shared.js into
src/index.html and src/history.html, writing dist/. Run after editing src/:

    python3 web/build.py
"""
import os

root = os.path.dirname(os.path.abspath(__file__))


def read(name):
    with open(os.path.join(root, "src", name)) as f:
        return f.read()


css, js = read("shared.css"), read("shared.js")
os.makedirs(os.path.join(root, "dist"), exist_ok=True)
for name in ["index.html", "history.html"]:
    page = read(name).replace("/*__SHARED_CSS__*/", css).replace("/*__SHARED_JS__*/", js)
    with open(os.path.join(root, "dist", name), "w") as f:
        f.write(page)
print("built web/dist")
