"""KiCad IPC plugin: a panel that diffs the open project straight from the
toolbar (see plugin/README.md). KiCad launches this file as its own process
when the button is clicked.

The panel asks for as little as possible, because a plugin already knows most
of it: KiCad tells it over the IPC API which project is open and which KiCad
version is running, the project's git repository supplies the commits, and the
board file supplies the layer list. What is left is two lists of checkboxes
(which commits, which layers) and where to put the result.

The diff itself is not reimplemented here - the panel builds the same command
line CLI users type and runs `python -m kdif` as a subprocess, streaming its
progress into the log box. Two reasons for a subprocess rather than calling
kdif.cli.main() in-process: a long export must not freeze the wx event loop,
and a failure inside kicad-cli handling can never take the panel (or KiCad's
plugin host) down with it.

UI is wxPython, not Tkinter: KiCad's plugin venv is created with
--system-site-packages, so it inherits the wx that KiCad itself is built on,
while python3-tk is generally absent.

Cross-platform notes, since this ships to Windows/macOS/Linux through the PCM
package (PCM/README.md):

* ``sys.executable`` is the plugin venv's interpreter on every platform; the
  bundled ``kdif`` package sits next to this file (PCM install) or one level
  up (dev checkout), and :func:`_plugin_root` picks whichever applies.
* kicad-cli is not on PATH on Windows or macOS. kdif's ``find_kicad_cli()``
  knows the standard install locations, and this panel passes it the running
  KiCad's major version so the matching one is picked when several are
  installed. Nothing to configure; ``$KICAD_CLI`` still overrides.
* Console-window flashes from the dozens of kicad-cli/git calls are suppressed
  inside kdif (``kdif/proc.py``), and for this panel's own subprocess by
  CREATE_NO_WINDOW below - Windows would otherwise pop up a console per call.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple

import wx


def _plugin_root() -> Path:
    """Directory to run kdif from - the one that has the ``kdif`` package in it.

    A PCM install has no repository checkout around it: PCM/create_pcm_archive.py
    bundles ``kdif/`` as a sibling of this file, so that layout is checked
    first. In a dev checkout ``plugin/`` is instead a sibling of ``kdif/``
    (typically symlinked into KiCad's plugin directory - see plugin/README.md).
    """
    here = Path(__file__).resolve().parent
    if (here / "kdif").is_dir():
        return here
    return here.parent


PLUGIN_ROOT = _plugin_root()
sys.path.insert(0, str(PLUGIN_ROOT))

from kdif import __version__ as KDIF_VERSION  # noqa: E402
from kdif import gitrepo  # noqa: E402
from kdif.exporter import ExportError, parse_board_layers, select_layers  # noqa: E402
from kdif.exporter import find_kicad_cli  # noqa: E402
from kdif.gitrepo import GitError, Rev  # noqa: E402

APP_TITLE = "kdif"

# How far back the commit picker looks. Only commits that touched the board or
# the schematic are listed, so this reaches a long way in project time.
COMMIT_LIMIT = 40

# Suppress the console window Windows would otherwise open for the child
# python process (same flag kdif/proc.py applies to git/kicad-cli).
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


# ------------------------------------------------------------------ KiCad IPC

def _project_from_document(doc) -> Optional[Path]:
    """The .kicad_pro of an open document, as reported over the IPC API.

    The API gives a project name and the path it lives at (which is the
    directory in some KiCad versions and the file in others), so both shapes
    are handled, with a glob of the directory as the last resort.
    """
    project = getattr(doc, "project", None)
    raw = getattr(project, "path", "") if project is not None else ""
    if not raw:
        return None
    path = Path(raw)
    if path.suffix == ".kicad_pro" and path.is_file():
        return path
    directory = path if path.is_dir() else path.parent
    name = getattr(project, "name", "") or ""
    if name:
        candidate = directory / (name if name.endswith(".kicad_pro") else name + ".kicad_pro")
        if candidate.is_file():
            return candidate
    found = sorted(directory.glob("*.kicad_pro"))
    return found[0] if found else None


def _probe_kicad() -> Tuple[Optional[Path], Optional[int]]:
    """Ask the running KiCad for the open project and its own major version.

    One connection for both, and never raises: everything it returns is
    optional context, and the panel reports the failure in its status line
    instead of dying with a traceback in a place nobody can read it.
    """
    project = None
    major = None
    try:
        from kipy import KiCad
        from kipy.proto.common.types import DocumentType
    except Exception:
        return None, None
    try:
        kicad = KiCad(timeout_ms=5000)
        try:
            major = int(getattr(kicad.get_version(), "major"))
        except Exception:
            major = None
        for doctype in (DocumentType.DOCTYPE_PCB, DocumentType.DOCTYPE_SCHEMATIC):
            for doc in kicad.get_open_documents(doctype):
                project = _project_from_document(doc)
                if project is not None:
                    return project, major
    except Exception:
        pass
    return project, major


class DiffFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=f"{APP_TITLE} {KDIF_VERSION}")
        self._proc = None            # type: Optional[subprocess.Popen]
        self._cancelled = False      # terminate() exit codes differ per platform
        self._out_path = None        # type: Optional[Path]
        self._log_lines = []         # type: List[str]
        self._log_current = ""       # last, still unterminated line (see _feed_log)
        self._project = None         # type: Optional[Path]
        self._kicad_cli = None       # type: Optional[List[str]]
        self._revs = []              # type: List[Rev]
        self._rev_names = []         # type: List[str]   # what to pass to kdif -r
        self._layers = []            # canonical layer names, parallel to the list box

        self._build_ui()
        self._load_context()

    # ------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        self.project_label = wx.StaticText(panel, label="Detecting the open project...",
                                           style=wx.ST_ELLIPSIZE_MIDDLE)
        outer.Add(self.project_label, 0, wx.EXPAND | wx.ALL, 10)

        lists = wx.BoxSizer(wx.HORIZONTAL)

        commits = wx.StaticBoxSizer(wx.VERTICAL, panel, "Compare (tick two or more)")
        self.commit_list = wx.CheckListBox(commits.GetStaticBox())
        self.commit_list.SetFont(
            wx.Font(wx.FontInfo().Family(wx.FONTFAMILY_TELETYPE)))
        # Both lists are still empty while the layout is computed, so their
        # size has to be stated outright - a best-size-driven layout would let
        # the empty layer list collapse to nothing next to the much wider
        # commit list.
        self.commit_list.SetMinSize(wx.Size(430, 150))
        commits.Add(self.commit_list, 1, wx.EXPAND | wx.ALL, 6)
        lists.Add(commits, 3, wx.EXPAND | wx.RIGHT, 8)

        layers = wx.StaticBoxSizer(wx.VERTICAL, panel, "Layers")
        self.layer_list = wx.CheckListBox(layers.GetStaticBox())
        self.layer_list.SetMinSize(wx.Size(160, 150))
        layers.Add(self.layer_list, 1, wx.EXPAND | wx.ALL, 6)
        layer_buttons = wx.BoxSizer(wx.HORIZONTAL)
        for label, value in (("All", True), ("None", False)):
            button = wx.Button(layers.GetStaticBox(), label=label, style=wx.BU_EXACTFIT)
            button.Bind(wx.EVT_BUTTON, lambda evt, v=value: self._check_all_layers(v))
            layer_buttons.Add(button, 1, wx.RIGHT, 4)
        layers.Add(layer_buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        # Proportion 0: layer names are short and of known length, so the box
        # keeps its width and every pixel a wider window brings goes to the
        # commit list, where the subject lines actually need it.
        lists.Add(layers, 0, wx.EXPAND)

        # Extra height goes to the pickers - the log only has to show the last
        # handful of lines while an export runs.
        outer.Add(lists, 3, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        output = wx.BoxSizer(wx.HORIZONTAL)
        output.Add(wx.StaticText(panel, label="Output:"), 0,
                   wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.output_ctrl = wx.TextCtrl(panel)
        output.Add(self.output_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        save_as = wx.Button(panel, label="Save as...")
        save_as.Bind(wx.EVT_BUTTON, self._on_browse_output)
        output.Add(save_as, 0)
        outer.Add(output, 0, wx.EXPAND | wx.ALL, 10)

        self.log_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.log_ctrl.SetMinSize(wx.Size(-1, 110))
        outer.Add(self.log_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self.status_label = wx.StaticText(panel, label="", style=wx.ST_ELLIPSIZE_MIDDLE)
        bottom.Add(self.status_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.open_btn = wx.Button(panel, label="Open result")
        self.open_btn.Bind(wx.EVT_BUTTON, lambda evt: self._open_result())
        self.open_btn.Disable()
        bottom.Add(self.open_btn, 0, wx.RIGHT, 6)
        self.run_btn = wx.Button(panel, label="Generate")
        self.run_btn.Bind(wx.EVT_BUTTON, self._on_run)
        self.run_btn.Disable()  # enabled by _load_context() once there is something to diff
        bottom.Add(self.run_btn, 0)
        outer.Add(bottom, 0, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(outer)
        # The frame gets a sizer of its own rather than relying on the
        # single-child auto-resize, and an explicit size rather than Fit():
        # fitting here would measure a layout whose two lists are still empty.
        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(frame_sizer)
        self.SetMinSize(wx.Size(820, 560))
        self.SetSize(wx.Size(960, 660))
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()

    def _set_status(self, text: str) -> None:
        self.status_label.SetLabel(text)

    def _check_all_layers(self, checked: bool) -> None:
        for i in range(self.layer_list.GetCount()):
            self.layer_list.Check(i, checked)

    # -------------------------------------------------------------- context

    def _load_context(self) -> None:
        """Fill in everything the panel can know without asking: project,
        commits, layers, kicad-cli. Any failure here is terminal for this
        window - Generate stays disabled and the status line says why."""
        try:
            self._load_context_inner()
        finally:
            # Both lists were empty when the layout was first computed; now
            # that they have content, lay it out again.
            self.Layout()

    def _load_context_inner(self) -> None:
        project, kicad_major = _probe_kicad()
        if project is None:
            self.project_label.SetLabel("No open project found")
            self._set_status("Open a board or schematic in KiCad, with "
                             "Preferences > Plugins > 'Enable KiCad API' turned on")
            return
        self._project = project
        self.project_label.SetLabel(f"Project: {project}")
        self.output_ctrl.SetValue(str(project.parent / f"{project.stem}-diff.html"))

        # Layers first: they come from a file on disk, so the box gets filled
        # (or says why it could not be) even when the git side below fails.
        self._load_layers(project)

        try:
            self._kicad_cli = find_kicad_cli(None, prefer_major=kicad_major)
            self._feed_log("kicad-cli: " + " ".join(self._kicad_cli) + "\n")
        except ExportError as e:
            self._kicad_cli = None
            self._feed_log(str(e) + "\n")
            self._set_status("kicad-cli not found - see the log")
            return

        try:
            self._load_commits(project)
        except GitError as e:
            # An empty picker says nothing about why it is empty, so the reason
            # goes where the commits would have been (as the layer box already
            # does); the full text - a missing git carries install hints over
            # several lines - goes to the log.
            self._feed_log(str(e) + "\n")
            lines = str(e).splitlines()
            self.commit_list.Set([f"({lines[0]})"])
            self.commit_list.Disable()
            self._set_status(lines[0] + (" - see the log" if len(lines) > 1 else ""))
            return

        self.run_btn.Enable()
        self._set_status(f"{len(self._revs)} commits touch this project - "
                         "tick the ones to compare")

    def _load_commits(self, project: Path) -> None:
        repo = gitrepo.repo_root(project.parent)
        # Only commits that touched the board or the schematic: a project's
        # history is mostly other files, and a commit that changed neither
        # produces an empty diff.
        docs = []
        for suffix in (".kicad_pcb", ".kicad_sch"):
            doc = project.with_suffix(suffix)
            if doc.is_file():
                try:
                    docs.append(doc.relative_to(repo).as_posix())
                except ValueError:
                    pass
        revs = gitrepo.commit_log(repo, COMMIT_LIMIT, docs or None)
        if len(revs) < 2:
            # Path filtering can be too strict (files renamed/moved into place);
            # an unfiltered history is more useful than an empty picker.
            revs = gitrepo.commit_log(repo, COMMIT_LIMIT)
        if len(revs) < 2:
            raise GitError(f"{repo} has fewer than two commits to compare")

        tags = gitrepo.tags_by_commit(repo)
        self._revs = revs
        self._rev_names = []
        columns = []
        for rev in revs:
            rev_tags = tags.get(rev.sha, [])
            # A tag is both a better label in the viewer and a more stable ref
            # than an abbreviated hash, so prefer it when the commit has one.
            self._rev_names.append(rev_tags[-1] if rev_tags else rev.short)
            columns.append(("[" + ", ".join(rev_tags) + "]") if rev_tags else "")
        # Tags sit right after the hash, in a column of their own: padded to the
        # widest tag so the date and subject stay aligned in the list's
        # monospaced font (and dropped entirely when nothing is tagged).
        width = max(len(c) for c in columns) if columns else 0
        labels = [
            f"{rev.short}  {column.ljust(width)}  {rev.date[:10]}  {rev.subject}"
            if width else f"{rev.short}  {rev.date[:10]}  {rev.subject}"
            for rev, column in zip(revs, columns)
        ]
        self.commit_list.Set(labels)
        # Default selection = what `kdif` does with no arguments: the two most
        # recent commits, so Generate is useful without touching anything.
        for i in (0, 1):
            self.commit_list.Check(i, True)

    def _load_layers(self, project: Path) -> None:
        board = project.with_suffix(".kicad_pcb")
        if not board.is_file():
            self.layer_list.Set(["(no board in this project)"])
            self.layer_list.Disable()
            return
        try:
            layers = select_layers(parse_board_layers(board), None)
        except ExportError as e:
            self.layer_list.Set([f"({e})"])
            self.layer_list.Disable()
            return
        self._layers = [layer.canonical for layer in layers]
        self.layer_list.Set([layer.display for layer in layers])
        self._check_all_layers(True)

    # ------------------------------------------------------------------ run

    def _on_browse_output(self, event: wx.CommandEvent) -> None:
        current = Path(self.output_ctrl.GetValue().strip() or ".")
        with wx.FileDialog(
            self, "Save the diff as",
            defaultDir=str(current.parent),
            defaultFile=current.name,
            wildcard="HTML (*.html)|*.html",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.output_ctrl.SetValue(dlg.GetPath())

    def _build_argv(self, output: Path, refs: List[str]) -> List[str]:
        argv = [sys.executable, "-u", "-m", "kdif", "-o", str(output)]
        for ref in refs:
            argv += ["-r", ref]
        checked = list(self.layer_list.GetCheckedItems()) if self._layers else []
        if self._layers and len(checked) != len(self._layers):
            argv += ["-l", ",".join(self._layers[i] for i in checked)]
        if self._kicad_cli:
            # Pass the resolved command through rather than letting the child
            # search again: it is the one matching the running KiCad.
            argv += ["--kicad-cli", " ".join(self._kicad_cli)]
        argv.append(str(self._project))
        return argv

    def _on_run(self, event: wx.CommandEvent) -> None:
        if self._proc is not None:
            self._cancelled = True
            self._proc.terminate()
            self._set_status("Cancelling...")
            return

        refs = [self._rev_names[i] for i in self.commit_list.GetCheckedItems()]
        if len(refs) < 2:
            wx.MessageBox("Tick at least two commits to compare.", APP_TITLE,
                          wx.OK | wx.ICON_INFORMATION)
            return
        if self._layers and not self.layer_list.GetCheckedItems():
            wx.MessageBox("Tick at least one layer.", APP_TITLE, wx.OK | wx.ICON_INFORMATION)
            return
        out_raw = self.output_ctrl.GetValue().strip()
        if not out_raw:
            wx.MessageBox("Choose where to write the HTML file.", APP_TITLE,
                          wx.OK | wx.ICON_INFORMATION)
            return

        self._out_path = Path(out_raw).expanduser()
        self._cancelled = False
        self._log_lines = []
        self._log_current = ""
        self.log_ctrl.ChangeValue("")
        self.open_btn.Disable()
        self.run_btn.SetLabel("Cancel")
        self._set_status("Running kicad-cli - this can take a while...")

        argv = self._build_argv(self._out_path, refs)
        self._feed_log("$ " + " ".join(argv) + "\n")
        # PYTHONPATH so `-m kdif` resolves to the bundled package regardless of
        # what the plugin venv has installed; cwd for the same reason.
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(PLUGIN_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        try:
            self._proc = subprocess.Popen(
                argv, cwd=str(PLUGIN_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, **_NO_WINDOW)
        except OSError as e:
            self._finish(1, f"could not start kdif: {e}")
            return
        threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        """Reader thread: raw reads (not readline) so the `\\r` progress line
        kdif prints during export shows up live instead of at the end."""
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            wx.CallAfter(self._feed_log, chunk.decode("utf-8", "replace"))
        code = proc.wait()
        wx.CallAfter(self._finish, code, None)

    def _feed_log(self, text: str) -> None:
        """Append output, letting `\\r` rewrite the current line like a terminal."""
        for ch in text:
            if ch == "\r":
                self._log_current = ""
            elif ch == "\n":
                self._log_lines.append(self._log_current)
                self._log_current = ""
            else:
                self._log_current += ch
        tail = [self._log_current] if self._log_current else []
        log = "\n".join(self._log_lines + tail)
        self.log_ctrl.ChangeValue(log)
        # Follow the output vertically, but scroll to the *start* of the last
        # line, not its end: showing the end drags the view sideways every time
        # a long line (the kicad-cli command, a path) arrives, and the text
        # then has to be scrolled back by hand to be readable.
        self.log_ctrl.ShowPosition(len(log) - len(log.rsplit("\n", 1)[-1]))

    def _finish(self, code: int, error: Optional[str]) -> None:
        self._proc = None
        self.run_btn.SetLabel("Generate")
        if error:
            self._feed_log(error + "\n")
            self._set_status(error)
            return
        exists = self._out_path is not None and self._out_path.is_file()
        if self._cancelled:
            self._set_status("Cancelled")
        elif code == 0 and exists:
            size_kib = self._out_path.stat().st_size / 1024
            self._set_status(f"Done: {self._out_path} ({size_kib:.0f} KiB)")
            # Nothing is opened automatically: the file is written where the
            # Output field says, and "Open result" shows it on request.
            self.open_btn.Enable()
        else:
            self._set_status(f"kdif failed (exit {code}) - see the log above")

    def _open_result(self) -> None:
        if self._out_path is not None and self._out_path.is_file():
            webbrowser.open(self._out_path.resolve().as_uri())

    def _on_close(self, event: wx.CloseEvent) -> None:
        if self._proc is not None:
            self._proc.terminate()
        event.Skip()


def main() -> None:
    # redirect=False: without it some wxPython builds capture stdout/stderr
    # into a popup window, which is exactly where kdif's progress output goes.
    app = wx.App(False)
    DiffFrame().Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
