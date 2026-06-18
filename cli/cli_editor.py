import os
import asyncio
from typing import Optional, List, Callable
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import (
    HSplit, VSplit, Window, ScrollOffsets, ConditionalContainer, WindowAlign
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from show_tree import show_tree # We will define this helper below or use standard logic
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.widgets import Frame
from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound

# --- Styles ---
EDITOR_STYLE = Style.from_dict({
    'status-bar': 'bg:#222222 #ffffff',
    'status-bar.key': 'bg:#222222 #00ff00 bold',
    'line-number': '#888888 bg:#111111',
    'cursor-line-number': '#ffffff bg:#0055aa',
    'search-toolbar': 'bg:#444444 #ffffff',
    'tree.selected': 'bg:#0055aa #ffffff',
    'tree.directory': '#aaaaaa bold',
    'tree.file': '#dddddd',
})

class FileTreeNavigator:
    def __init__(self, root_path: str, on_select: Callable):
        self.root_path = os.path.abspath(root_path)
        self.on_select = on_select
        self.current_index = 0
        self.files: List[dict] = []
        self.refresh_tree()
        
        # Buffer for the tree (read-only)
        self.buffer = Buffer(read_only=True)
        self.control = FormattedTextControl(
            text=self._get_formatted_text,
            focusable=True,
            key_bindings=self._get_tree_bindings()
        )
        self.window = Window(
            content=self.control,
            width=Dimension(min=20, max=40, preferred=30),
            scroll_offsets=ScrollOffsets(top=2, bottom=2),
        )

    def refresh_tree(self):
        """Scans directory and builds a flat list for navigation"""
        self.files = []
        for dirpath, dirnames, filenames in os.walk(self.root_path):
            # Filter out hidden dirs and __pycache__
            dirnames[:] = [d for d in dirnames if not d.startswith('.') and d != '__pycache__']
            
            level = dirpath.replace(self.root_path, '').count(os.sep)
            indent = '  ' * level
            
            if dirpath != self.root_path:
                dirname = os.path.basename(dirpath)
                self.files.append({'type': 'dir', 'name': dirname, 'path': dirpath, 'indent': indent})
            
            for filename in sorted(filenames):
                if filename.startswith('.'): continue
                fpath = os.path.join(dirpath, filename)
                self.files.append({'type': 'file', 'name': filename, 'path': fpath, 'indent': indent})

    def _get_formatted_text(self):
        result = []
        for i, item in enumerate(self.files):
            is_selected = (i == self.current_index)
            style = ''
            if is_selected:
                style = 'class:tree.selected'
            elif item['type'] == 'dir':
                style = 'class:tree.directory'
            else:
                style = 'class:tree.file'
            
            icon = "📁 " if item['type'] == 'dir' else "📄 "
            result.append((style, f"{item['indent']}{icon}{item['name']}\n"))
        return FormattedText(result)

    def _get_tree_bindings(self):
        kb = KeyBindings()
        
        @kb.add('up')
        def _(event):
            if self.current_index > 0:
                self.current_index -= 1
                event.app.invalidate()

        @kb.add('down')
        def _(event):
            if self.current_index < len(self.files) - 1:
                self.current_index += 1
                event.app.invalidate()

        @kb.add('enter')
        def _(event):
            if self.files:
                selected = self.files[self.current_index]
                if selected['type'] == 'file':
                    self.on_select(selected['path'])
                    # Optionally close tree or switch focus
                    event.app.layout.focus(self.editor_window_ref) 
        return kb

    # Helper to link editor window later
    editor_window_ref = None

class CodeEditorPane:
    def __init__(self):
        self.current_file_path: Optional[str] = None
        self.buffer = Buffer()
        
        # Dynamic Lexer
        self.lexer = None
        
        self.control = BufferControl(
            buffer=self.buffer,
            lexer=self.lexer,
            include_line_numbers=True,
        )
        
        self.window = Window(
            content=self.control,
            scroll_offsets=ScrollOffsets(top=3, bottom=3),
            wrap_lines=True,
        )
        
        # Store reference for Tree to focus back
        FileTreeNavigator.editor_window_ref = self.window

    def open_file(self, path: str):
        self.current_file_path = path
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            content = f"# Error reading file: {e}"
        
        self.buffer.text = content
        
        # Update Lexer based on extension
        try:
            self.lexer = PygmentsLexer(get_lexer_for_filename(path))
        except ClassNotFound:
            self.lexer = None # Plain text
            
        self.control.lexer = self.lexer

    def update_content(self, new_content: str):
        """Called by Agent to update file in real-time"""
        # Preserve cursor position if possible, or just update text
        cursor_pos = self.buffer.cursor_position
        self.buffer.text = new_content
        # Try to restore cursor or move to end
        if cursor_pos <= len(new_content):
            self.buffer.cursor_position = cursor_pos
        else:
            self.buffer.cursor_position = len(new_content)

class StatusBar:
    def __init__(self, app_ref):
        self.app_ref = app_ref
        self.show_tree = True
        self.text_control = FormattedTextControl(text=self.get_text)
        self.window = Window(
            height=1,
            content=self.text_control,
            style='class:status-bar'
        )

    def get_text(self):
        # Dynamic hints based on state
        toggle_tree_key = "Ctrl+H" if self.show_tree else "Ctrl+H"
        tree_status = "[Nav: ON]" if self.show_tree else "[Nav: OFF]"
        
        return FormattedText([
            ('class:status-bar.key', f" {toggle_tree_key} "),
            ('', f"Toggle Nav {tree_status}  "),
            ('class:status-bar.key', ' Ctrl+S '),
            ('', 'Save  '),
            ('class:status-bar.key', ' Ctrl+Q '),
            ('', 'Quit  '),
            ('', f" | File: {self.app_ref.editor.current_file_path or 'No file'}"),
        ])

class CLIEditorApp:
    def __init__(self, root_path: str = "."):
        self.root_path = root_path
        self.editor = CodeEditorPane()
        self.tree = FileTreeNavigator(root_path, on_select=self.editor.open_file)
        self.status_bar = StatusBar(self) # Pass self to access editor state
        
        # Initial Layout Construction
        # We use ConditionalContainer to show/hide the tree
        self.tree_container = ConditionalContainer(
            content=Frame(self.tree.window, title="File Navigation"),
            condition=lambda: self.status_bar.show_tree
        )

        self.main_layout = HSplit([
            VSplit([
                self.tree_container,
                Window(width=1, char='│', style='class:line-number'), # Separator
                Frame(self.editor.window, title="Editor"),
            ]),
            self.status_bar.window,
        ])

        self.kb = self._get_bindings()
        
        self.app = Application(
            layout=Layout(self.main_layout),
            key_bindings=self.kb,
            full_screen=True,
            style=EDITOR_STYLE,
            mouse_support=True, # Enable mouse scrolling/clicking
        )

    def _get_bindings(self):
        kb = KeyBindings()

        @kb.add('c-h') # Ctrl+H to toggle tree (Changed from N to avoid conflict, or keep c-n)
        def _(event):
            self.status_bar.show_tree = not self.status_bar.show_tree
            event.app.invalidate()

        @kb.add('c-s')
        def _(event):
            if self.editor.current_file_path:
                try:
                    with open(self.editor.current_file_path, 'w') as f:
                        f.write(self.editor.buffer.text)
                    # Flash save message?
                except Exception as e:
                    pass # Handle error UI

        @kb.add('c-q')
        def _(event):
            event.app.exit()

        return kb

    def run(self):
        self.app.run()

    def update_file_content(self, filepath: str, content: str):
        """External method for Agent to push updates"""
        if self.editor.current_file_path == filepath:
            self.editor.update_content(content)
            # If app is running in thread, we need to invalidate
            if self.app.is_running:
                self.app.invalidate()

# Helper to run in background if needed by agent
def launch_editor(root_path: str = ".") -> CLIEditorApp:
    app = CLIEditorApp(root_path)
    # Run in a separate thread if the agent needs to block? 
    # Usually prompt_toolkit takes over the main thread.
    # For integration, the Agent should call app.run() as its main loop.
    return app
