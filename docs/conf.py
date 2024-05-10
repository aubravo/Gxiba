import os

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.coverage",
    "sphinx.ext.doctest",
    "sphinx.ext.extlinks",
    "sphinx.ext.ifconfig",
    "sphinx.ext.napoleon",
    "sphinx.ext.todo",
    "sphinx.ext.viewcode",
    "sphinx_autodoc_typehints",
]
source_suffix = ".rst"
master_doc = "index"
project = "earthgazer"
year = "2021-2024"
author = "Alvaro Bravo"
copyright = f"{year}, {author}"
version = release = "1.0.0"

pygments_style = "one-dark"
templates_path = ["."]
extlinks = {
    "issue": ("https://github.com/aubravo/earthgazer/issues/%s", "#"),
    "pr": ("https://github.com/aubravo/earthgazer/pull/%s", "PR #"),
}
# on_rtd is whether we are on readthedocs.org
on_rtd = os.environ.get("READTHEDOCS", None) == "True"

if not on_rtd:  # only set the theme if we are building docs locally
    html_theme = "sphinx_rtd_theme"

html_use_smartypants = True
html_last_updated_fmt = "%b %d, %Y"
html_split_index = False
html_sidebars = {
    "**": ["searchbox.html", "globaltoc.html", "sourcelink.html"],
}
html_short_title = f"{project}-{version}"

napoleon_use_ivar = True
napoleon_use_rtype = False
napoleon_use_param = False
napoleon_include_private_with_doc = False
