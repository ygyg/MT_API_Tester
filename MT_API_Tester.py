# mt_api_tester.py
#!/usr/bin/env python3

import sys   
import asyncio
import json
import logging
import os
import pytz
import re
import threading
from datetime import datetime
from queue import Queue
from tkinter import ttk, scrolledtext, messagebox
import tkinter as tk
import websockets

import nest_asyncio
nest_asyncio.apply()

class JsonFormatter:
    """Handles JSON formatting and timezone conversion"""
    
    @classmethod
    def convert_timestamps(cls, obj):
        """Recursively convert UTC timestamps to NY time"""
        ny_tz = pytz.timezone('America/New_York')

        if isinstance(obj, dict):
            return {key: cls.convert_timestamps(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [cls.convert_timestamps(item) for item in obj]
        elif isinstance(obj, str) and cls.is_iso_datetime(obj):
            try:
                dt = datetime.fromisoformat(obj.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = pytz.utc.localize(dt)
                ny_time = dt.astimezone(ny_tz)
                return ny_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            except ValueError:
                return obj
        return obj

    @classmethod
    def format_json_response(cls, message: str) -> str:
        """Format JSON string with proper indentation and timezone conversion"""
        if not cls.is_valid_json(message):
            return message

        try:
            data = json.loads(message)
            data = cls.convert_timestamps(data)
            return json.dumps(data, indent=2)
        except Exception as e:
            return f"Error formatting JSON: {str(e)}\nOriginal message:\n{message}"

    @staticmethod
    def is_iso_datetime(dt_str: str) -> bool:
        """Check if string matches ISO datetime format"""
        iso_pattern = re.compile(
            r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?$'
        )
        return bool(iso_pattern.match(dt_str))

    @staticmethod
    def is_valid_json(data_str: str | bytes | bytearray | memoryview) -> bool:
        """Check if the input string is valid JSON."""
        try:
            json.loads(data_str)
            return True
        except json.JSONDecodeError:
            return False

class JsonText(tk.Text):
    """Custom Text widget for JSON input with auto-formatting support"""
    def __init__(self, master, font_size=10, **kwargs):
        kwargs.setdefault('height', 4)
        kwargs.setdefault('width', 60)
        kwargs.setdefault('font', ('Courier', font_size))
        super().__init__(master, **kwargs)
        
        self.configure(wrap='none')
        self.configure(padx=5, pady=5)
        
        self.bind('<KeyRelease>', self._on_key_release)
        

    def fix_json_format(self, text: str) -> str:
        """Fix common JSON formatting issues"""
        def quote_properties(match):
            return f'"{match.group(1)}": '
            
        text = re.sub(r'([a-zA-Z_]\w*)\s*:', quote_properties, text)
        text = text.replace("'", '"')
        return text

    def format_json(self, content: str) -> str:
        """Format JSON content with proper indentation"""
        try:
            fixed_content = self.fix_json_format(content)
            parsed = json.loads(fixed_content)
            return json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            return content

    def get_json(self):
        """Get the JSON content"""
        return self.get("1.0", "end-1c").strip()


    def _on_key_release(self, event):
        """Handle key release events for JSON formatting"""
        if event.keysym in ('Return', 'Tab'):
            content = self.get("1.0", "end-1c").strip()
            if content:
                formatted = self.format_json(content)
                if formatted != content:
                    self.delete("1.0", "end")
                    self.insert("1.0", formatted)
                    


class TextHandler(logging.Handler):
    """Handler for logging to tkinter Text widget"""
    def __init__(self, text):
        logging.Handler.__init__(self)
        self.text = text

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text.configure(state='normal')
            self.text.insert(tk.END, msg + "\n")
            self.text.configure(state='disabled')
            self.text.yview(tk.END)
        self.text.after(0, append)
        

class AsyncWebsocketManager:
    """Manages asynchronous WebSocket connections"""
    
    def __init__(self, url, on_message, on_connect, on_disconnect):
        self.url = url
        self.websocket = None
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.running = False
        self.loop = None
        self.thread = None
        self.send_queue = Queue()
        self._cleanup_event = threading.Event()
        self._cleanup_lock = threading.Lock()
        self._is_cleaning_up = False

    async def connect(self):
        """Establish WebSocket connection"""
        try:
            self.websocket = await websockets.connect(self.url)
            self.running = True
            self.on_connect()
            await self.message_loop()
        except Exception as e:
            self.on_disconnect(str(e))
        finally:
            self.running = False

    async def message_loop(self):
        """Main message loop for sending and receiving messages"""
        try:
            while self.running and self.websocket:
                # Handle sending messages
                while not self.send_queue.empty() and self.running:
                    message = self.send_queue.get()
                    try:
                        await self.websocket.send(message)
                    except Exception as e:
                        logging.error(f"Failed to send message: {e}")
                    finally:
                        self.send_queue.task_done()

                # Handle receiving messages
                try:
                    message = await asyncio.wait_for(self.websocket.recv(), timeout=0.1)
                    if message:
                        self.on_message(message)
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
                except Exception as e:
                    logging.error(f"Error in message loop: {e}")
                    break

        except Exception as e:
            self.on_disconnect(str(e))
        finally:
            self.running = False
            if self.websocket:
                try:
                    await self.websocket.close()
                except Exception as e:
                    logging.error(f"Error closing websocket: {e}")

    async def _cleanup_loop(self):
        """Clean up the event loop"""
        try:
            tasks = [t for t in asyncio.all_tasks(self.loop) 
                    if t is not asyncio.current_task()]
            
            # Cancel all tasks
            for task in tasks:
                task.cancel()
                
            # Wait for tasks to complete with a timeout
            try:
                await asyncio.wait(tasks, timeout=2.0)
            except asyncio.TimeoutError:
                pass  # Some tasks may not complete, but we continue cleanup
                
        except Exception as e:
            logging.error(f"Error cleaning up tasks: {e}")

    async def _close_websocket(self):
        """Safely close the websocket connection"""
        if self.websocket:
            try:
                # Check if websocket is already closed
                if not self.websocket.closed:
                    await self.websocket.close()
                    # Wait briefly for close to complete
                    await asyncio.sleep(0.1)
            except websockets.exceptions.ConnectionClosed:
                pass  # Already closed
            except Exception as e:
                logging.error(f"Error in _close_websocket: {e}")
            finally:
                self.websocket = None

    def send_message(self, message):
        """Add message to send queue"""
        if self.running:
            self.send_queue.put(message)

    def start(self):
        """Start the WebSocket connection in a separate thread"""
        def run_loop():
            try:
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                self.loop.run_until_complete(self.connect())
            except Exception as e:
                logging.error(f"Error in websocket thread: {e}")
            finally:
                try:
                    if not self.loop.is_closed():
                        self.loop.close()
                except Exception as e:
                    logging.error(f"Error closing event loop: {e}")
                self.loop = None

        self.thread = threading.Thread(target=run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop the WebSocket connection and clean up resources"""
        with self._cleanup_lock:
            if self._is_cleaning_up:
                return
            self._is_cleaning_up = True
        
        try:
            # Signal stop
            self.running = False
            self._cleanup_event.set()
            
            # Clear message queue
            while not self.send_queue.empty():
                try:
                    self.send_queue.get_nowait()
                    self.send_queue.task_done()
                except Exception:
                    break

            if self.websocket:
                if self.loop and self.loop.is_running():
                    # If we have a running loop, use it for cleanup
                    try:
                        fut = asyncio.run_coroutine_threadsafe(
                            self._close_websocket(), 
                            self.loop
                        )
                        # Wait with timeout for websocket to close
                        fut.result(timeout=1.0)
                    except Exception as e:
                        logging.error(f"Error closing websocket in running loop: {e}")
                else:
                    # If no running loop, create a new one for cleanup
                    temp_loop = asyncio.new_event_loop()
                    try:
                        temp_loop.run_until_complete(self._close_websocket())
                    except Exception as e:
                        logging.error(f"Error closing websocket in temp loop: {e}")
                    finally:
                        temp_loop.close()

            # Stop the event loop if it's still running
            if self.loop and self.loop.is_running():
                try:
                    self.loop.call_soon_threadsafe(self.loop.stop)
                except Exception as e:
                    logging.error(f"Error stopping loop: {e}")

            # Wait for thread to finish with timeout
            if self.thread and self.thread.is_alive():
                try:
                    self.thread.join(timeout=1.0)
                except Exception as e:
                    logging.error(f"Error joining thread: {e}")

            # Final cleanup
            self.websocket = None
            self.loop = None
            self.thread = None
            
        except Exception as e:
            logging.error(f"Error during stop: {e}")
        finally:
            self._is_cleaning_up = False
            self._cleanup_event.clear()
            self.on_disconnect("Disconnected")  # Notify about disconnection

            
class MedvedTraderAPIClient:
    """Main application class for MT API testing"""
    
    VERSION = "1.0.0"
    
    def __init__(self, master):
        self.master = master
        
        self.input_update_lock = False  # Initialize the lock flag
        self.current_edit_frame = None  # Initialize current edit frame reference
    
        self._cleanup_lock = threading.Lock()
        self._is_cleaning_up = False
        
        # Set up paths first
        self.app_data_path = self.get_app_data_path()
        # Create directory if it doesn't exist
        os.makedirs(self.app_data_path, exist_ok=True)     
        
        self.cache_file = os.path.join(self.app_data_path, "api_tester_cache.json")
        
        # Connection variables
        self.server_url = "ws://127.0.0.1:16400"
        self.is_authenticated = False
        
        # Set up styles
        self.setup_styles()        
        
        # Set up logging
        self.response_text = scrolledtext.ScrolledText(
            self.master, 
            wrap=tk.WORD, 
            width=80,
            height=40
        )
        self.logger = self.setup_logger()        
        
        # Load configuration including password
        self.load_config()
        
 
        
        # Load stored text and settings from cache
        self.load_cache()



        # UI elements initialization lists
        self.input_frames = []
        self.input_fields = []
        self.send_buttons = []

        
        # Create UI elements
        self.create_widgets()
        
        # WebSocket manager
        self.ws_manager = AsyncWebsocketManager(
            self.server_url,
            self.on_message,
            self.on_connect,
            self.on_disconnect
        )

        # Handle window closing
        self.master.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Setup autosave
        self.setup_autosave()
        


    def add_row(self):
        """Add a new row at the bottom"""
        self.create_input_row()
        self.update_row_numbers()


    def _bind_mousewheel(self):
        """Bind mousewheel events to the canvas"""
        def _on_mousewheel(event):
            # Get canvas area coordinates
            canvas_x = self.canvas.winfo_rootx()
            canvas_y = self.canvas.winfo_rooty()
            canvas_width = self.canvas.winfo_width()
            canvas_height = self.canvas.winfo_height()
            
            # Get mouse position
            mouse_x = self.master.winfo_pointerx()
            mouse_y = self.master.winfo_pointery()
            
            # Check if mouse is within canvas area
            if (canvas_x <= mouse_x <= canvas_x + canvas_width and 
                canvas_y <= mouse_y <= canvas_y + canvas_height):
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        
        # Unbind any existing mousewheel bindings
        self.canvas.unbind_all("<MouseWheel>")
        
        # Bind mousewheel to the canvas
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)
        

    def clear_response_text(self):
        """Clear the response text area"""
        self.response_text.configure(state='normal')
        self.response_text.delete("1.0", tk.END)
        self.response_text.configure(state='disabled')


    def cleanup_resources(self):
        """Clean up all application resources"""
        with self._cleanup_lock:
            if self._is_cleaning_up:
                return
            self._is_cleaning_up = True
        
        try:
            # Save cache before cleanup
            try:
                self.save_cache()
            except Exception as e:
                self.log_error(f"Error saving cache during cleanup: {e}")

            # Stop WebSocket
            if hasattr(self, 'ws_manager') and self.ws_manager:
                try:
                    self.logger.info("Stopping WebSocket connection...")
                    self.ws_manager.stop()
                    self.log_debug("WebSocket connection stopped")
                except Exception as e:
                    self.log_error(f"Error stopping WebSocket: {e}")

            # Clean up logging
            if hasattr(self, 'logger') and self.logger:
                try:
                    self.log_debug("Cleaning up logging handlers...")
                    handlers = self.logger.handlers[:]
                    for handler in handlers:
                        try:
                            handler.flush()
                            handler.close()
                            self.logger.removeHandler(handler)
                        except Exception as e:
                            self.log_error(f"Error closing log handler: {e}")
                except Exception as e:
                    self.log_error(f"Error cleaning up logging: {e}")

        except Exception as e:
            self.log_error(f"Error during resource cleanup: {e}")
        finally:
            self._is_cleaning_up = False
            
            
    def _configure_font_styles(self, font_size):
        """Configure ttk styles with given font size"""
        style = ttk.Style()
        style.configure('MT.TButton', font=('Courier', font_size))
        style.configure('MT.TLabel', font=('Courier', font_size))
        style.configure('MT.TLabelframe.Label', font=('Courier', font_size))
        style.configure('MT.TLabelframe', font=('Courier', font_size))            

    def create_font_controls(self, header_frame):
        """Create font size control buttons"""
        font_frame = ttk.Frame(header_frame)
        font_frame.pack(side=tk.LEFT, padx=10)
        
        # Font size label with style
        ttk.Label(font_frame, text="Font Size:", 
                  style='MT.TLabel').pack(side=tk.LEFT, padx=2)
        
        # Control buttons with style
        decrease_btn = ttk.Button(font_frame, text="-", width=2,
                                 command=lambda: self.update_font_size(self.font_size - 1),
                                 style='MT.TButton')
        decrease_btn.pack(side=tk.LEFT, padx=1)
        
        increase_btn = ttk.Button(font_frame, text="+", width=2,
                                 command=lambda: self.update_font_size(self.font_size + 1),
                                 style='MT.TButton')
        increase_btn.pack(side=tk.LEFT, padx=1)
    
    
    def create_input_row(self, initial_text=""):
        """Create a new input row with single-line text widget and control buttons"""
        frame = ttk.Frame(self.scrollable_frame)
        frame.grid_columnconfigure(1, weight=1)
        
        # Control buttons frame
        controls_frame = ttk.Frame(frame)
        controls_frame.grid(row=0, column=0, padx=(2, 4), sticky="w")
        
        # Row number label with font size
        row_num = len(self.input_frames) + 1
        row_label = ttk.Label(controls_frame, text=f"{row_num}:", width=2,
                             font=('Courier', self.font_size))
        row_label.pack(side=tk.LEFT, padx=(0, 2))

        # Control buttons
        control_buttons = [
            ("+", lambda: self.insert_row_after(frame)),
            ("×", lambda: self.delete_specific_row(frame)),
            ("↑", lambda: self.move_specific_row(frame, -1)),
            ("↓", lambda: self.move_specific_row(frame, 1))
        ]
        
        for text, command in control_buttons:
            btn = ttk.Button(controls_frame, text=text, width=1, 
                            style='MT.TButton', command=command)
            btn.pack(side=tk.LEFT, padx=1)

        # Single-line input field with font size
        input_field = ttk.Entry(frame, font=('Courier', self.font_size))
        input_field.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        
        # Set up input variable and trace
        input_var = tk.StringVar()
        input_field.configure(textvariable=input_var)
        
        # Store references
        frame.input_var = input_var
        frame.full_content = initial_text or ""
        frame.input_field = input_field  # Store reference for font updates
        frame.row_label = row_label  # Store reference for font updates

        # Set initial content
        if initial_text:
            if initial_text.lstrip().startswith('#'):
                # If starts with comment, show first line
                first_line = initial_text.split('\n')[0]
                input_var.set(first_line)
            else:
                # If no comment, show all text in one line
                display_text = ' '.join(initial_text.split('\n'))
                input_var.set(display_text)

        def on_input_change(*args):
            if not hasattr(self, 'input_update_lock'):
                self.input_update_lock = False
            
            if self.input_update_lock:
                return
                
            self.input_update_lock = True
            try:
                new_text = input_var.get()
                frame.full_content = new_text
                
                if hasattr(self, 'current_edit_frame') and self.current_edit_frame == frame:
                    editor_content = self.request_editor.get("1.0", "end-1c")
                    if editor_content.lstrip().startswith('#'):
                        lines = editor_content.split('\n')
                        lines[0] = new_text
                        updated_content = '\n'.join(lines)
                    else:
                        updated_content = new_text
                        
                    self.request_editor.delete("1.0", "end")
                    self.request_editor.insert("1.0", updated_content)
            finally:
                self.input_update_lock = False

        input_var.trace_add("write", on_input_change)
        input_field.bind('<Button-1>', lambda e, f=frame: self.show_in_editor(f))

        # Send button
        send_button = ttk.Button(
            frame, 
            text="Send",
            style='MT.TButton',
            width=6,
            command=lambda f=frame: self.send_json(self.get_row_index(f))
        )
        send_button.grid(row=0, column=2, sticky="e")

        # Add to lists and grid it
        row_idx = len(self.input_frames)
        frame.grid(row=row_idx, column=0, sticky="ew", pady=2)

        self.input_frames.append(frame)
        self.input_fields.append(input_field)
        self.send_buttons.append(send_button)
        
        return frame
    
      
    def create_widgets(self):
        """Create all UI widgets"""
        # Configure main window grid
        self.master.grid_rowconfigure(0, weight=0)  # Header
        self.master.grid_rowconfigure(1, weight=1)  # Main content
        self.master.grid_columnconfigure(0, weight=1)  # Left panel
        self.master.grid_columnconfigure(1, weight=1)  # Right panel

        # Header Frame
        header_frame = ttk.Frame(self.master)
        header_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=5, pady=5)

        # URL Label with style
        self.url_label = ttk.Label(header_frame, text=f"Server URL: {self.server_url}",
                                  style='MT.TLabel')
        self.url_label.pack(side=tk.LEFT, padx=5)

        # Connect button with style
        self.connect_button = ttk.Button(header_frame, text="Connect", 
                                       command=self.start_connect,
                                       style='MT.TButton')
        self.connect_button.pack(side=tk.LEFT, padx=5)

        # Clear button with style
        self.clear_button = ttk.Button(header_frame, text="Clear", 
                                     command=self.clear_response_text,
                                     style='MT.TButton')
        self.clear_button.pack(side=tk.LEFT, padx=5)

        # Add font controls
        self.create_font_controls(header_frame)

        # Response Text
        self.response_text.configure(font=('Courier', self.font_size))
        self.response_text.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        # Right Panel with PanedWindow
        right_panel = ttk.Frame(self.master)
        right_panel.grid(row=1, column=1, sticky="nsew", padx=5, pady=5)
        right_panel.grid_rowconfigure(0, weight=1)
        right_panel.grid_columnconfigure(0, weight=1)

        # Create vertical PanedWindow for right panel
        paned = ttk.PanedWindow(right_panel, orient=tk.VERTICAL)
        paned.grid(row=0, column=0, sticky="nsew")

        # Upper frame for input rows
        upper_frame = ttk.Frame(paned)
        upper_frame.grid_columnconfigure(0, weight=1)
        upper_frame.grid_rowconfigure(0, weight=1)

        # Create canvas and scrollbar for input rows
        self.canvas = tk.Canvas(upper_frame)
        scrollbar_y = ttk.Scrollbar(upper_frame, orient="vertical", 
                                   command=self.canvas.yview)

        # Configure the canvas
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.grid_columnconfigure(0, weight=1)

        self.canvas_frame = self.canvas.create_window(
            (0, 0),
            window=self.scrollable_frame,
            anchor="nw",
            width=self.canvas.winfo_width()
        )

        # Configure canvas scrolling
        self.scrollable_frame.bind("<Configure>", self.on_frame_configure)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self._bind_mousewheel()        

        # Grid the canvas and scrollbar
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar_y.grid(row=0, column=1, sticky="ns")

        self.canvas.configure(yscrollcommand=scrollbar_y.set)

        # Lower frame for request editor with styled label
        editor_frame = ttk.LabelFrame(paned, text="Request Editor", style='MT.TLabelframe')
        editor_frame.grid_columnconfigure(0, weight=1)
        editor_frame.grid_rowconfigure(0, weight=1)

        self.request_editor = JsonText(editor_frame, font_size=self.font_size)
        self.request_editor.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Add frames to PanedWindow with initial sizes
        paned.add(upper_frame, weight=2)  # Upper frame gets 2/3 of space
        paned.add(editor_frame, weight=1)  # Lower frame gets 1/3 of space

        # Create initial rows
        for i in range(min(10, len(self.stored_text))):
            self.create_input_row(self.stored_text[i])

            
    def delete_row(self):
        """Delete the selected row"""
        if len(self.input_frames) <= 1:  # Keep at least one row
            return
            
        # Get the currently focused input field
        focused = self.master.focus_get()
        delete_idx = -1
        
        # Find the index of the focused input field
        for i, field in enumerate(self.input_fields):
            if field is focused:  # Remove tooltip check
                delete_idx = i
                break
        
        if delete_idx >= 0:
            # Remove widgets
            self.input_frames[delete_idx].destroy()
            self.input_frames.pop(delete_idx)
            self.input_fields.pop(delete_idx)
            self.send_buttons.pop(delete_idx)
            
            self.update_row_numbers()
            
    def delete_specific_row(self, frame):
        """Delete a specific row by its frame reference"""
        if len(self.input_frames) <= 1:  # Keep at least one row
            # Clear the text of the last remaining row
            self.input_fields[0].delete(0, tk.END)
            return
            
        idx = self.get_row_index(frame)
        if idx >= 0:
            # If this is the last row in the list, clear the text of the previous row
            if idx == len(self.input_frames) - 1 and idx > 0:
                self.input_fields[idx - 1].delete("1.0", tk.END)
                
            # Remove widgets
            frame.destroy()
            self.input_frames.pop(idx)
            self.input_fields.pop(idx)
            self.send_buttons.pop(idx)
            
            # Update grid positions
            self.regrid_all_rows()
            self.update_row_numbers()   



    def get_app_data_path(self):
        """Get the application data directory path"""
        # Get the directory where the script is located
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # Create path for .mt_api_tester subfolder
        return os.path.join(script_dir, ".mt_api_tester")

    def get_config_path(self):
        """Get the configuration file path"""
        return os.path.join(self.app_data_path, "mt_api_tester_config.json")

    def get_json_content(self, input_field):
        """Get JSON content from either Entry or Text widget"""
        if isinstance(input_field, tk.Entry):
            # For Entry widget, get the full content from parent frame
            frame_idx = self.input_fields.index(input_field)
            frame = self.input_frames[frame_idx]
            return getattr(frame, 'full_content', '')
        else:
            # For Text widget (like request_editor)
            return input_field.get("1.0", "end-1c").strip()            

    def get_password_from_user(self):
        """Prompt user for MT password and save to config"""
        from tkinter import simpledialog, messagebox
        
        try:
            # Create password dialog as child of main window
            pwd = simpledialog.askstring("Password Required", 
                                       "Enter Medved Trader Password:", 
                                       parent=self.master,  # Specify parent window
                                       show='*')
            
            if pwd is None:  # User clicked Cancel or closed dialog
                self.log_debug("Password entry cancelled by user")
                self.ws_manager.stop()  # Cleanly disconnect
                self.connect_button.configure(text="Connect")
                return None
                
            if pwd.strip():  # Valid password entered
                self.mt_password = pwd
                self.save_config()
                return pwd
            else:  # Empty password
                messagebox.showwarning("Invalid Password", 
                                     "Password cannot be empty.",
                                     parent=self.master)
                self.ws_manager.stop()
                self.connect_button.configure(text="Connect")
                return None
                
        except Exception as e:
            self.log_error(f"Error during password entry: {e}")
            self.ws_manager.stop()
            self.connect_button.configure(text="Connect")
            return None

    def get_row_index(self, frame):
        """Get the index of a row by its frame reference"""
        try:
            return self.input_frames.index(frame)
        except ValueError:
            return -1        

    def insert_row(self):
        """Insert a new row at the selected position"""
        # Get the currently focused input field
        focused = self.master.focus_get()
        insert_idx = 0
        
        # Find the index of the focused input field
        for i, field in enumerate(self.input_fields):
            if field is focused:
                insert_idx = i
                break
        
        # Create new row widgets
        new_frame = self.create_input_row()
        
        # Move the new frame to the correct position
        new_frame.pack_forget()
        new_frame.pack(in_=self.scrollable_frame, fill=tk.X, pady=2, 
                      before=self.input_frames[insert_idx])
        
        # Update lists
        self.input_frames.insert(insert_idx, self.input_frames.pop())
        self.input_fields.insert(insert_idx, self.input_fields.pop())
        self.send_buttons.insert(insert_idx, self.send_buttons.pop())
        
        self.update_row_numbers()
        
    def insert_row_after(self, frame):
        """Insert a new row after the specified frame"""
        insert_idx = self.get_row_index(frame) + 1
        
        # Create new row widgets
        new_frame = self.create_input_row("")
        
        if insert_idx < len(self.input_frames):
            # Move lists
            self.input_frames.insert(insert_idx, self.input_frames.pop())
            self.input_fields.insert(insert_idx, self.input_fields.pop())
            self.send_buttons.insert(insert_idx, self.send_buttons.pop())
            
            # Update grid positions
            self.regrid_all_rows()
        
        self.update_row_numbers()     
        


    def load_cache(self):
        """Load cached settings and requests"""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    # Load requests with validation
                    stored = data.get('requests', [])
                    if isinstance(stored, list) and all(isinstance(x, str) for x in stored):
                        self.stored_text = (stored + [""] * 10)[:10]
                    else:
                        self.stored_text = [""] * 10
                        
                    # Load settings
                    settings = data.get('settings', {})
                    if isinstance(settings, dict):
                        self.server_url = settings.get('server_url', self.server_url)
            else:
                self.stored_text = [""] * 10
                
        except Exception as e:
            self.log_error(f"Error loading cache: {e}")
            self.stored_text = [""] * 10
            
    def load_config(self):
        """Load configuration from file"""
        config_file = self.get_config_path()
        
        self.log_debug(f"Loading config from: {config_file}")
        
        try:
            if os.path.exists(config_file):
                self.log_debug("Config file exists, reading...")
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.mt_password = config.get('mt_password', '')
                    self.server_url = config.get('server_url', self.server_url)
                    self.font_size = config.get('font_size', 10)  # Default font size is 10
                    self.log_debug(f"Loaded config: server_url={self.server_url}, font_size={self.font_size}")
            else:
                self.log_debug("Config file does not exist, creating with defaults")
                self.mt_password = ''
                self.font_size = 10
                self.save_config()  # Create initial config file
                
        except Exception as e:
            self.log_error(f"Error loading config: {e}")
            self.mt_password = ''
            self.font_size = 10
            
    def log_debug(self, message):
        """Log debug message - goes to console and file only"""
        self.logger.debug(message)

    def log_api(self, message):
        """Log API message - goes to response widget, file, and console"""
        self.logger.info(message)

    def log_error(self, message):
        """Log error message - goes to console and file only"""
        self.logger.error(message)            
            
    def move_row(self, direction):
        """Move the selected row up or down"""
        # Get the currently focused input field
        focused = self.master.focus_get()
        move_idx = -1
        
        # Find the index of the focused input field
        for i, field in enumerate(self.input_fields):
            if field is focused:  # Remove tooltip check here
                move_idx = i
                break
        
        if move_idx >= 0:
            new_idx = move_idx + direction
            if 0 <= new_idx < len(self.input_frames):
                # Swap frames in the UI
                frame = self.input_frames[move_idx]
                target_frame = self.input_frames[new_idx]
                
                frame.pack_forget()
                target_frame.pack_forget()
                
                if direction < 0:  # Moving up
                    frame.pack(in_=self.scrollable_frame, before=target_frame, 
                             fill=tk.X, pady=2)
                    target_frame.pack(in_=self.scrollable_frame, fill=tk.X, pady=2)
                else:  # Moving down
                    target_frame.pack(in_=self.scrollable_frame, fill=tk.X, pady=2)
                    frame.pack(in_=self.scrollable_frame, before=target_frame, 
                             fill=tk.X, pady=2)
                
                # Update lists
                self.input_frames[move_idx], self.input_frames[new_idx] = \
                    self.input_frames[new_idx], self.input_frames[move_idx]
                self.input_fields[move_idx], self.input_fields[new_idx] = \
                    self.input_fields[new_idx], self.input_fields[move_idx]
                self.send_buttons[move_idx], self.send_buttons[new_idx] = \
                    self.send_buttons[new_idx], self.send_buttons[move_idx]
                
                self.update_row_numbers()
                
                # Set focus to the moved field
                self.input_fields[new_idx].focus_set()

            
    def move_specific_row(self, frame, direction):
        """Move a specific row up or down"""
        current_idx = self.get_row_index(frame)
        new_idx = current_idx + direction
        
        if 0 <= new_idx < len(self.input_frames):
            # Swap lists
            self.input_frames[current_idx], self.input_frames[new_idx] = \
                self.input_frames[new_idx], self.input_frames[current_idx]
            self.input_fields[current_idx], self.input_fields[new_idx] = \
                self.input_fields[new_idx], self.input_fields[current_idx]
            self.send_buttons[current_idx], self.send_buttons[new_idx] = \
                self.send_buttons[new_idx], self.send_buttons[current_idx]
            
            # Update grid positions
            self.regrid_all_rows()
            
            # Update row numbers
            self.update_row_numbers()
            
            # Set focus to the moved field
            self.input_fields[new_idx].focus_set()               
            


    def on_canvas_configure(self, event):
        """When canvas is resized, resize the frame within it"""
        self.canvas.itemconfig(self.canvas_frame, width=event.width)
           

    def on_closing(self):
            """Handle application shutdown"""
            try:
                # Disable all input
                if hasattr(self, 'connect_button'):
                    self.connect_button['state'] = 'disabled'
                
                for button in getattr(self, 'send_buttons', []):
                    button['state'] = 'disabled'

                # Clean up resources
                self.cleanup_resources()

                # Destroy the root window
                try:
                    self.master.quit()
                    self.master.destroy()
                except Exception as e:
                    print(f"Error destroying window: {e}")

            except Exception as e:
                print(f"Error during shutdown: {e}")
            finally:
                # Force exit
                print("Exiting application...")
                try:
                    import os
                    os._exit(0)
                except Exception:
                    import sys
                    sys.exit(1)            

    def on_connect(self):
        """Handle WebSocket connection"""
        self.log_debug("Connected to server")
        self.is_authenticated = False  # Reset auth state
        self.master.after(0, lambda: self.connect_button.configure(text="Disconnect"))
        
        # Send connect command on next event loop iteration
        self.master.after(100, self.send_connect_command)

    def on_disconnect(self, reason):
        """Handle WebSocket disconnection"""
        self.log_debug(f"Disconnected: {reason}")
        self.is_authenticated = False
        self.master.after(0, lambda: self.connect_button.configure(text="Connect"))

    def on_editor_change(self, event=None):
        """Handle changes in the request editor"""
        if not self.current_edit_frame:  # Simplified check
            return
            
        if self.input_update_lock:
            return
            
        self.input_update_lock = True
        try:
            # Get updated content
            content = self.request_editor.get("1.0", "end-1c")
            
            # Update frame's stored content
            self.current_edit_frame.full_content = content
            
            # Update single-line display
            if hasattr(self.current_edit_frame, 'input_var'):
                if content.lstrip().startswith('#'):
                    # If starts with comment, show first line
                    first_line = content.split('\n')[0]
                    self.current_edit_frame.input_var.set(first_line)
                else:
                    # If no comment, show all text in one line
                    display_text = ' '.join(content.split('\n'))
                    self.current_edit_frame.input_var.set(display_text)
        finally:
            self.input_update_lock = False       
            
    def on_frame_configure(self, event=None):
        """Reset the scroll region to encompass the inner frame"""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        
    def on_message(self, message):
        """Handle received WebSocket messages"""
        # Skip heartbeat messages
        if "heartbeat" in message.lower():
            return

        # Handle plain text messages
        if message == "CONNECTED":
            self.log_api("Received connection confirmation")
            return

        # Handle JSON messages
        try:
            msg_data = json.loads(message)
            cmd = msg_data.get("cmd")
            
            # Handle connect response
            if cmd == "connect":
                if msg_data.get("success") == "OK":
                    self.is_authenticated = True
                    self.log_api("Successfully authenticated with MT server")
                else:
                    self.is_authenticated = False
                    error = msg_data.get("result", {}).get("reason", "Unknown error")
                    self.log_api(f"Authentication failed: {error}")
                    # Clear password so user can try again
                    self.mt_password = ""

            # Format and log the message
            formatted_message = JsonFormatter.format_json_response(message)
            self.log_api(f"Received:\n{formatted_message}")

        except json.JSONDecodeError:
            # For non-JSON messages that aren't the CONNECTED message
            self.log_api(f"Received plain text message: {message}")


    def regrid_all_rows(self):
        """Update grid positions for all rows"""
        for idx, frame in enumerate(self.input_frames):
            frame.grid(row=idx, column=0, sticky="ew", pady=2)    

                
    def save_cache(self):
        """Save settings and requests to cache"""
        try:
            # Get current text from all input fields
            if hasattr(self, 'input_fields'):
                self.stored_text = [field.get_json() for field in self.input_fields]
            
            cache_data = {
                'version': self.VERSION,
                'last_modified': datetime.now().isoformat(),
                'requests': self.stored_text,
                'settings': {
                    'server_url': self.server_url,
                }
            }
            
            # Create backup of existing cache
            if os.path.exists(self.cache_file):
                backup_file = f"{self.cache_file}.bak"
                try:
                    import shutil
                    shutil.copy2(self.cache_file, backup_file)
                except Exception as e:
                    self.log_error(f"Error creating cache backup: {e}")
            
            # Save new cache
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2)
                
        except Exception as e:
            self.log_error(f"Error saving cache: {e}")

    def save_config(self):
        """Save configuration to file"""
        config_file = self.get_config_path()
        
        self.log_debug(f"Saving config to: {config_file}")
        try:
            config = {
                'mt_password': self.mt_password,
                'server_url': self.server_url,
                'font_size': self.font_size
            }
            
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
                
            self.log_debug(f"Saved config: server_url={self.server_url}, font_size={self.font_size}")
                
        except Exception as e:
            self.log_error(f"Error saving config: {e}")
            
            
    def setup_autosave(self):
        """Setup periodic autosave"""
        def autosave():
            self.save_cache()
            # Schedule next autosave in 30 seconds
            self.master.after(30000, autosave)
            
        # Start the autosave cycle
        self.master.after(30000, autosave)

    def setup_logger(self):
        """Setup logging configuration"""
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)  # Set to lowest level, let handlers filter

        # Console handler - for development/debug messages
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)  # Show debug messages in console
        console_formatter = logging.Formatter('%(levelname)s: %(message)s')
        console_handler.setFormatter(console_formatter)
        console_handler.addFilter(lambda record: record.levelno in [logging.DEBUG, logging.ERROR])
        logger.addHandler(console_handler)

        # File handler - for full logging
        file_handler = logging.FileHandler(os.path.join(self.app_data_path, "api_tester.log"))
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        # Text widget handler - for API communication
        text_handler = TextHandler(self.response_text)
        text_handler.setLevel(logging.INFO)  # Only show INFO level (API messages) in UI
        text_formatter = logging.Formatter('%(asctime)s - %(message)s')  # Simplified format for UI
        text_handler.setFormatter(text_formatter)
        # Only show API communication in the UI text widget
        text_handler.addFilter(lambda record: 
            "Sent:" in record.msg or 
            "Received:" in record.msg or
            "Connected" in record.msg or
            "Disconnected" in record.msg)
        logger.addHandler(text_handler)

        return logger
        
        
    def send_connect_command(self):
        """Send the initial connect command with password"""
        if not self.mt_password:
            # Prompt for password if not set
            self.mt_password = self.get_password_from_user()
            if not self.mt_password:
                return False
                    
        connect_cmd = {
            "cmd": "connect",
            "pwd": self.mt_password,
            "reqID": "connect-1"
        }
        
        try:
            self.ws_manager.send_message(json.dumps(connect_cmd))
            return True
        except Exception as e:
            self.log_error(f"Failed to send connect command: {e}")
            return False

    def send_json(self, index):
        """Send JSON from the specified input field"""
        if not self.is_authenticated:
            self.log_debug("Not authenticated. Please connect with valid password first.")
            return
            
        frame = self.input_frames[index]
        content = getattr(frame, 'full_content', '')
        
        # Skip empty content
        if not content.strip():
            self.log_debug(f"Input field {index+1} is empty.")
            return
            
        # Remove comment line if present
        lines = content.split('\n')
        if lines[0].strip().startswith('#'):
            content = '\n'.join(lines[1:])
            
        # Skip if no content after removing comment
        if not content.strip():
            self.log_debug(f"Input field {index+1} contains only comments.")
            return
            
        try:
            # Validate JSON
            json.loads(content)
            
            # Send message
            self.ws_manager.send_message(content)
            formatted_message = JsonFormatter.format_json_response(content)
            self.log_api(f"Sent:\n{formatted_message}")
        except json.JSONDecodeError as e:
            self.log_error(f"Invalid JSON in input field {index+1}: {e}")

        
            
    def setup_styles(self):
        """Setup ttk styles"""
        self._configure_font_styles(self.font_size)
    
    def show_in_editor(self, frame):
        """Show the content of the selected input in the request editor"""
        # Clear any previous content and unbind previous handler
        self.request_editor.delete("1.0", tk.END)
        self.request_editor.unbind('<<Modified>>')
        self.request_editor.unbind('<KeyRelease>')
        
        # Insert the full content
        content = getattr(frame, 'full_content', '')
        self.request_editor.insert("1.0", content)
        
        # Store reference to current frame being edited
        self.current_edit_frame = frame
        
        # Bind to KeyRelease for real-time updates
        self.request_editor.bind('<KeyRelease>', self.on_editor_change)         



    def start_connect(self):
        """Handle connect/disconnect button click"""
        if self.connect_button["text"] == "Connect":
            self.connect_button["state"] = tk.DISABLED
            self.ws_manager.start()
            self.connect_button["state"] = tk.NORMAL
        else:
            self.connect_button["state"] = tk.DISABLED
            try:
                self.ws_manager.stop()
            finally:
                self.connect_button["state"] = tk.NORMAL
                self.connect_button.configure(text="Connect")
                self.is_authenticated = False

    def update_font_size(self, new_size=None):
        """Update font size for all text widgets"""
        if new_size is not None:
            if new_size < 6:  # Minimum size limit
                new_size = 6
            elif new_size > 24:  # Maximum size limit
                new_size = 24
            
            self.font_size = new_size
            self.save_config()
        
        # Update all ttk styles
        self._configure_font_styles(self.font_size)

        # Update response text area
        self.response_text.configure(font=('Courier', self.font_size))
        
        # Update request editor
        self.request_editor.configure(font=('Courier', self.font_size))
        
        # Update input rows
        for frame in self.input_frames:
            # Update input field font
            if hasattr(frame, 'input_field'):
                frame.input_field.configure(font=('Courier', self.font_size))
                
            # Update row number label font
            if hasattr(frame, 'row_label'):
                frame.row_label.configure(font=('Courier', self.font_size))

    def update_row_numbers(self):
        """Update the row numbers for all rows"""
        for i, frame in enumerate(self.input_frames):
            label = frame.winfo_children()[0].winfo_children()[0]  # Get label widget
            label.configure(text=f"{i+1}:")

def main():
    """Main program entry point"""
    try:
        root = tk.Tk()
        root.title("Medved Trader API Client")
        
        # Set minimum window size
        root.minsize(1200, 800)
        
        # Set initial window size
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        window_width = min(1600, screen_width - 100)
        window_height = min(1000, screen_height - 100)
        
        # Center the window
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        root.geometry(f"{window_width}x{window_height}+{x}+{y}")

        # Set window icon (if available)
        try:
            if os.name == 'nt':  # Windows
                root.iconbitmap('mt_icon.ico')
            else:  # Linux/Mac
                img = tk.PhotoImage(file='mt_icon.png')
                root.tk.call('wm', 'iconphoto', root._w, img)
        except Exception:
            pass  # Icon not found, use default

        # Create and start the application
        app = MedvedTraderAPIClient(root)
        
        # Start the main event loop
        root.mainloop()
        
    except Exception as e:
        print(f"Error starting application: {e}")
        messagebox.showerror("Error", f"Failed to start application:\n{str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()            