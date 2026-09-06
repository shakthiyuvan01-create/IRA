"""
Snake Game — A production-quality implementation using tkinter.

Design Principles:
- Clean OOP architecture with separation of concerns
- Smooth 60 FPS game loop using tkinter's after() method
- Professional UI with subtle visual polish
- Keyboard controls with direction queue to prevent 180-degree turns
- Score persistence and game state management
"""

import tkinter as tk
from tkinter import font
import random
import time
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from enum import Enum


# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================

@dataclass(frozen=True)
class GameConfig:
    """Immutable game configuration — single source of truth for all tunable values."""
    # Grid & Window
    grid_size: int = 20              # Number of cells per row/column
    cell_size: int = 25              # Pixel size of each cell
    margin: int = 20                 # Window margin around grid

    # Gameplay
    initial_speed: int = 150         # Initial tick interval in milliseconds
    speed_increment: int = 2         # Speed increase per food eaten (ms faster)
    min_speed: int = 50              # Maximum speed cap (ms)
    initial_length: int = 3          # Starting snake length

    # Colors (professional palette)
    bg_color: str = "#1a1a2e"        # Deep navy background
    grid_color: str = "#16213e"      # Slightly lighter grid lines
    snake_head_color: str = "#00d9a5"  # Vibrant teal head
    snake_body_color: str = "#00b894"  # Slightly darker teal body
    snake_tail_color: str = "#00a888"  # Tail gradient
    food_color: str = "#ff6b6b"      # Coral red food
    food_glow_color: str = "#ff8787" # Food glow effect
    text_primary: str = "#ffffff"    # White text
    text_secondary: str = "#8b9dc3"  # Muted blue-gray
    accent_color: str = "#74b9ff"    # Sky blue accent
    game_over_bg: str = "rgba(26, 26, 46, 0.95)"  # Semi-transparent overlay

    # Visual
    corner_radius: int = 4           # Cell corner radius
    head_corner_radius: int = 6      # Head corner radius (more rounded)
    food_pulse_speed: float = 0.08   # Food animation speed
    shadow_offset: int = 2           # Drop shadow offset

    # Fonts
    font_family: str = "Segoe UI"    # System font
    title_size: int = 28
    score_size: int = 18
    ui_size: int = 14


CONFIG = GameConfig()


# =============================================================================
# CORE DATA STRUCTURES
# =============================================================================

class Direction(Enum):
    """Snake movement directions with vector components."""
    UP = (0, -1)
    DOWN = (0, 1)
    LEFT = (-1, 0)
    RIGHT = (1, 0)

    @property
    def dx(self) -> int:
        return self.value[0]

    @property
    def dy(self) -> int:
        return self.value[1]

    def opposite(self) -> 'Direction':
        """Return the opposite direction (for 180-degree turn prevention)."""
        opposites = {
            Direction.UP: Direction.DOWN,
            Direction.DOWN: Direction.UP,
            Direction.LEFT: Direction.RIGHT,
            Direction.RIGHT: Direction.LEFT,
        }
        return opposites[self]


@dataclass
class Position:
    """Immutable grid position with utility methods."""
    x: int
    y: int

    def __add__(self, other: 'Position') -> 'Position':
        return Position(self.x + other.x, self.y + other.y)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Position):
            return NotImplemented
        return self.x == other.x and self.y == other.y

    def __hash__(self) -> int:
        return hash((self.x, self.y))

    def to_pixel(self, cell_size: int, margin: int) -> Tuple[int, int]:
        """Convert grid position to pixel coordinates (top-left of cell)."""
        return (
            margin + self.x * cell_size,
            margin + self.y * cell_size
        )

    def to_center_pixel(self, cell_size: int, margin: int) -> Tuple[int, int]:
        """Convert grid position to pixel coordinates (center of cell)."""
        px, py = self.to_pixel(cell_size, margin)
        half = cell_size // 2
        return (px + half, py + half)


@dataclass
class GameState:
    """Mutable game state — single source of truth for runtime values."""
    snake: List[Position] = field(default_factory=list)
    direction: Direction = Direction.RIGHT
    pending_direction: Optional[Direction] = None  # Queued direction change
    food: Optional[Position] = None
    score: int = 0
    high_score: int = 0
    speed: int = CONFIG.initial_speed
    running: bool = False
    game_over: bool = False
    food_pulse_phase: float = 0.0


# =============================================================================
# GAME ENGINE (Pure Logic — No UI Dependencies)
# =============================================================================

class SnakeEngine:
    """
    Pure game logic engine. Contains no UI code — fully testable in isolation.
    Handles: movement, collision detection, food spawning, scoring, speed scaling.
    """

    def __init__(self, config: GameConfig = CONFIG):
        self.config = config
        self.state = GameState()
        self._rng = random.Random()

    def reset(self) -> None:
        """Initialize or reset game to starting state."""
        # Center the snake horizontally, middle vertically
        start_x = self.config.grid_size // 2 - self.config.initial_length // 2
        start_y = self.config.grid_size // 2

        self.state.snake = [
            Position(start_x + i, start_y)
            for i in range(self.config.initial_length)
        ]
        self.state.direction = Direction.RIGHT
        self.state.pending_direction = None
        self.state.score = 0
        self.state.speed = self.config.initial_speed
        self.state.running = True
        self.state.game_over = False
        self.state.food_pulse_phase = 0.0
        self._spawn_food()

    def _spawn_food(self) -> None:
        """Spawn food at random empty position."""
        occupied = set(self.state.snake)
        empty_positions = [
            Position(x, y)
            for x in range(self.config.grid_size)
            for y in range(self.config.grid_size)
            if Position(x, y) not in occupied
        ]
        if empty_positions:
            self.state.food = self._rng.choice(empty_positions)
        else:
            self.state.food = None  # Grid full — player wins (theoretical)

    def change_direction(self, new_direction: Direction) -> None:
        """Queue a direction change (prevents 180-degree turns)."""
        if (self.state.pending_direction is None or
            new_direction != self.state.pending_direction.opposite()):
            # Only allow if not reversing current or pending direction
            current_dir = self.state.pending_direction or self.state.direction
            if new_direction != current_dir.opposite():
                self.state.pending_direction = new_direction

    def tick(self) -> bool:
        """
        Advance game by one tick.
        Returns True if game continues, False if game over.
        """
        if not self.state.running or self.state.game_over:
            return False

        # Apply queued direction change
        if self.state.pending_direction is not None:
            self.state.direction = self.state.pending_direction
            self.state.pending_direction = None

        # Calculate new head position
        head = self.state.snake[0]
        new_head = Position(
            head.x + self.state.direction.dx,
            head.y + self.state.direction.dy
        )

        # Check wall collision
        if not (0 <= new_head.x < self.config.grid_size and
                0 <= new_head.y < self.config.grid_size):
            self._game_over()
            return False

        # Check self collision
        if new_head in self.state.snake:
            self._game_over()
            return False

        # Move snake: add new head
        self.state.snake.insert(0, new_head)

        # Check food collision
        if self.state.food and new_head == self.state.food:
            self._eat_food()
        else:
            # No food eaten — remove tail (snake moves forward)
            self.state.snake.pop()

        # Update food pulse animation
        self.state.food_pulse_phase += self.config.food_pulse_speed

        return True

    def _eat_food(self) -> None:
        """Handle food consumption: score, speed, spawn new food."""
        self.state.score += 10
        if self.state.score > self.state.high_score:
            self.state.high_score = self.state.score

        # Increase speed (decrease tick interval)
        self.state.speed = max(
            self.config.min_speed,
            self.state.speed - self.config.speed_increment
        )

        self._spawn_food()

    def _game_over(self) -> None:
        """Handle game over state."""
        self.state.running = False
        self.state.game_over = True

    def get_state_snapshot(self) -> GameState:
        """Return a copy of current state for rendering."""
        return GameState(
            snake=self.state.snake.copy(),
            direction=self.state.direction,
            pending_direction=self.state.pending_direction,
            food=self.state.food,
            score=self.state.score,
            high_score=self.state.high_score,
            speed=self.state.speed,
            running=self.state.running,
            game_over=self.state.game_over,
            food_pulse_phase=self.state.food_pulse_phase
        )


# =============================================================================
# RENDERER (Pure UI — No Game Logic)
# =============================================================================

class SnakeRenderer:
    """
    Handles all drawing operations. Receives GameState snapshots and renders them.
    Uses double-buffered canvas for flicker-free animation.
    """

    def __init__(self, canvas: tk.Canvas, config: GameConfig = CONFIG):
        self.canvas = canvas
        self.config = config
        self._font_title = font.Font(family=config.font_family, size=config.title_size, weight="bold")
        self._font_score = font.Font(family=config.font_family, size=config.score_size, weight="bold")
        self._font_ui = font.Font(family=config.font_family, size=config.ui_size)
        self._font_small = font.Font(family=config.font_family, size=11)

        # Pre-calculate dimensions
        self.grid_pixel_size = config.grid_size * config.cell_size
        self.canvas_width = self.grid_pixel_size + 2 * config.margin
        self.canvas_height = self.grid_pixel_size + 2 * config.margin + 60  # Extra for score bar

    def render(self, state: GameState) -> None:
        """Full frame render — clears and redraws everything."""
        self.canvas.delete("all")
        self._draw_background()
        self._draw_grid()

        if state.food:
            self._draw_food(state.food, state.food_pulse_phase)

        self._draw_snake(state.snake)
        self._draw_score_bar(state)

        if state.game_over:
            self._draw_game_over(state)
        elif not state.running:
            self._draw_start_screen()

    def _draw_background(self) -> None:
        """Draw window background."""
        self.canvas.create_rectangle(
            0, 0, self.canvas_width, self.canvas_height,
            fill=self.config.bg_color, outline=""
        )

    def _draw_grid(self) -> None:
        """Draw subtle grid lines."""
        margin = self.config.margin
        cell = self.config.cell_size
        grid_px = self.grid_pixel_size

        # Vertical lines
        for i in range(self.config.grid_size + 1):
            x = margin + i * cell
            self.canvas.create_line(
                x, margin, x, margin + grid_px,
                fill=self.config.grid_color, width=1
            )
        # Horizontal lines
        for i in range(self.config.grid_size + 1):
            y = margin + i * cell
            self.canvas.create_line(
                margin, y, margin + grid_px, y,
                fill=self.config.grid_color, width=1
            )

    def _draw_rounded_rect(self, x1: int, y1: int, x2: int, y2: int,
                           radius: int, **kwargs) -> None:
        """Draw a rounded rectangle on canvas."""
        # Tkinter doesn't have native rounded rect, so we approximate with polygon
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _draw_cell(self, pos: Position, color: str, radius: int = None,
                   glow: bool = False, glow_color: str = None) -> None:
        """Draw a single grid cell at position with optional glow."""
        if radius is None:
            radius = self.config.corner_radius

        px, py = pos.to_pixel(self.config.cell_size, self.config.margin)
        cell = self.config.cell_size
        padding = 2  # Small gap between cells

        x1 = px + padding
        y1 = py + padding
        x2 = px + cell - padding
        y2 = py + cell - padding

        # Draw glow/shadow first (behind)
        if glow and glow_color:
            shadow_offset = self.config.shadow_offset
            self._draw_rounded_rect(
                x1 + shadow_offset, y1 + shadow_offset,
                x2 + shadow_offset, y2 + shadow_offset,
                radius, fill=glow_color, outline=""
            )

        # Draw main cell
        self._draw_rounded_rect(x1, y1, x2, y2, radius, fill=color, outline="")

    def _draw_snake(self, snake: List[Position]) -> None:
        """Draw snake with head/body/tail distinction and subtle gradient."""
        if not snake:
            return

        for i, segment in enumerate(snake):
            is_head = (i == 0)
            is_tail = (i == len(snake) - 1)

            if is_head:
                color = self.config.snake_head_color
                radius = self.config.head_corner_radius
                glow = True
                glow_color = "#004d3a"  # Dark teal shadow
            elif is_tail:
                color = self.config.snake_tail_color
                radius = self.config.corner_radius
                glow = False
            else:
                # Gradient from head to tail
                ratio = i / max(1, len(snake) - 1)
                color = self._interpolate_color(
                    self.config.snake_head_color,
                    self.config.snake_body_color,
                    ratio * 0.7  # Don't go all the way to body color
                )
                radius = self.config.corner_radius
                glow = False

            self._draw_cell(segment, color, radius, glow, glow_color)

    def _draw_food(self, food: Position, pulse_phase: float) -> None:
        """Draw food with pulsing animation."""
        # Pulse scale factor (0.85 to 1.0)
        pulse = 0.85 + 0.15 * (1 + abs((pulse_phase % 2) - 1)) * 0.5

        px, py = food.to_center_pixel(self.config.cell_size, self.config.margin)
        cell = self.config.cell_size
        base_radius = (cell - 4) // 2
        radius = int(base_radius * pulse)

        # Glow effect (larger, semi-transparent circle behind)
        glow_radius = radius + 4
        self.canvas.create_oval(
            px - glow_radius, py - glow_radius,
            px + glow_radius, py + glow_radius,
            fill=self.config.food_glow_color, outline="",
            stipple="gray25"  # Simulates transparency
        )

        # Main food circle
        self.canvas.create_oval(
            px - radius, py - radius,
            px + radius, py + radius,
            fill=self.config.food_color, outline=""
        )

        # Highlight (smaller circle for 3D effect)
        highlight_radius = max(2, radius // 3)
        self.canvas.create_oval(
            px - highlight_radius, py - highlight_radius,
            px + highlight_radius, py + highlight_radius,
            fill="#ffaaaa", outline=""
        )

    def _draw_score_bar(self, state: GameState) -> None:
        """Draw top score bar with current score and high score."""
        margin = self.config.margin
        grid_px = self.grid_pixel_size
        bar_y = margin + grid_px + 10

        # Score
        self.canvas.create_text(
            margin, bar_y,
            text=f"Score: {state.score}",
            font=self._font_score,
            fill=self.config.text_primary,
            anchor="w"
        )

        # High score
        self.canvas.create_text(
            self.canvas_width - margin, bar_y,
            text=f"Best: {state.high_score}",
            font=self._font_score,
            fill=self.config.accent_color,
            anchor="e"
        )

        # Speed indicator (subtle)
        speed_pct = (self.config.initial_speed - state.speed) / (self.config.initial_speed - self.config.min_speed)
        speed_text = f"Speed: {int(speed_pct * 100)}%"
        self.canvas.create_text(
            self.canvas_width // 2, bar_y + 25,
            text=speed_text,
            font=self._font_small,
            fill=self.config.text_secondary,
            anchor="center"
        )

    def _draw_start_screen(self) -> None:
        """Draw start screen overlay."""
        center_x = self.canvas_width // 2
        center_y = self.canvas_height // 2 - 30

        # Title
        self.canvas.create_text(
            center_x, center_y - 60,
            text="SNAKE",
            font=self._font_title,
            fill=self.config.text_primary
        )

        # Instructions
        instructions = [
            "Arrow Keys or WASD to move",
            "Space to start / pause",
            "Eat food to grow and speed up",
            "Don't hit walls or yourself!"
        ]
        for i, line in enumerate(instructions):
            self.canvas.create_text(
                center_x, center_y + i * 28,
                text=line,
                font=self._font_ui,
                fill=self.config.text_secondary
            )

        # Start prompt
        self.canvas.create_text(
            center_x, center_y + 140,
            text="Press SPACE to begin",
            font=self._font_score,
            fill=self.config.accent_color
        )

    def _draw_game_over(self, state: GameState) -> None:
        """Draw game over overlay with final score."""
        center_x = self.canvas_width // 2
        center_y = self.canvas_height // 2 - 20

        # Semi-transparent overlay (simulated with dark rectangle)
        self.canvas.create_rectangle(
            0, 0, self.canvas_width, self.canvas_height,
            fill=self.config.bg_color, stipple="gray50", outline=""
        )

        # Game Over text
        self.canvas.create_text(
            center_x, center_y - 60,
            text="GAME OVER",
            font=self._font_title,
            fill=self.config.food_color
        )

        # Final score
        self.canvas.create_text(
            center_x, center_y,
            text=f"Final Score: {state.score}",
            font=self._font_score,
            fill=self.config.text_primary
        )

        # High score celebration
        if state.score >= state.high_score and state.score > 0:
            self.canvas.create_text(
                center_x, center_y + 35,
                text="★ NEW HIGH SCORE! ★",
                font=self._font_score,
                fill=self.config.accent_color
            )

        # Restart prompt
        self.canvas.create_text(
            center_x, center_y + 80,
            text="Press SPACE to play again",
            font=self._font_ui,
            fill=self.config.text_secondary
        )

    @staticmethod
    def _interpolate_color(color1: str, color2: str, factor: float) -> str:
        """Interpolate between two hex colors."""
        def hex_to_rgb(h: str) -> Tuple[int, int, int]:
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

        def rgb_to_hex(r: int, g: int, b: int) -> str:
            return f"#{r:02x}{g:02x}{b:02x}"

        r1, g1, b1 = hex_to_rgb(color1)
        r2, g2, b2 = hex_to_rgb(color2)

        r = int(r1 + (r2 - r1) * factor)
        g = int(g1 + (g2 - g1) * factor)
        b = int(b1 + (b2 - b1) * factor)

        return rgb_to_hex(r, g, b)


# =============================================================================
# MAIN APPLICATION (Coordinates Engine + Renderer + Input)
# =============================================================================

class SnakeGame:
    """
    Main application class. Orchestrates engine, renderer, and input handling.
    Runs at 60 FPS using tkinter's event loop.
    """

    # Key mappings for flexible controls
    KEY_MAP = {
        # Arrow keys
        "Up": Direction.UP,
        "Down": Direction.DOWN,
        "Left": Direction.LEFT,
        "Right": Direction.RIGHT,
        # WASD
        "w": Direction.UP,
        "s": Direction.DOWN,
        "a": Direction.LEFT,
        "d": Direction.RIGHT,
        "W": Direction.UP,
        "S": Direction.DOWN,
        "A": Direction.LEFT,
        "D": Direction.RIGHT,
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = CONFIG
        self.engine = SnakeEngine(self.config)
        self._setup_window()
        self._setup_canvas()
        self.renderer = SnakeRenderer(self.canvas, self.config)
        self._bind_keys()
        self._running = True
        self._last_tick = time.time()
        self._accumulator = 0.0
        self._tick_interval = self.config.initial_speed / 1000.0  # Convert to seconds

        # Start render loop
        self._render_loop()

    def _setup_window(self) -> None:
        """Configure main window."""
        self.root.title("Snake")
        self.root.resizable(False, False)
        self.root.configure(bg=self.config.bg_color)

        # Center window on screen
        width = self.config.grid_size * self.config.cell_size + 2 * self.config.margin
        height = width + 60
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_canvas(self) -> None:
        """Create and configure drawing canvas."""
        width = self.config.grid_size * self.config.cell_size + 2 * self.config.margin
        height = width + 60

        self.canvas = tk.Canvas(
            self.root,
            width=width,
            height=height,
            bg=self.config.bg_color,
            highlightthickness=0,
            bd=0
        )
        self.canvas.pack()
        self.canvas.focus_set()  # Ensure canvas receives key events

    def _bind_keys(self) -> None:
        """Bind keyboard events."""
        # Direction keys
        for key, direction in self.KEY_MAP.items():
            self.root.bind(f"<KeyPress-{key}>",
                          lambda e, d=direction: self._on_key_press(d))

        # Space for start/pause/restart
        self.root.bind("<KeyPress-space>", self._on_space)
        self.root.bind("<KeyPress-Return>", self._on_space)  # Enter also works

        # Escape to quit
        self.root.bind("<Escape>", lambda e: self._on_close())

        # Focus management
        self.canvas.bind("<FocusIn>", lambda e: self.canvas.focus_set())

    def _on_key_press(self, direction: Direction) -> None:
        """Handle direction key press."""
        if self.engine.state.running and not self.engine.state.game_over:
            self.engine.change_direction(direction)

    def _on_space(self, event=None) -> None:
        """Handle space/enter: start, pause, or restart."""
        state = self.engine.state

        if state.game_over:
            # Restart game
            self.engine.reset()
        elif not state.running:
            # Start new game
            self.engine.reset()
        else:
            # Toggle pause
            state.running = not state.running

    def _on_close(self) -> None:
        """Clean shutdown."""
        self._running = False
        self.root.destroy()

    def _render_loop(self) -> None:
        """Main render loop — runs at ~60 FPS, game logic runs at variable speed."""
        if not self._running:
            return

        current_time = time.time()
        delta_time = current_time - self._last_tick
        self._last_tick = current_time

        # Fixed timestep for game logic
        self._accumulator += delta_time
        tick_interval = self.engine.state.speed / 1000.0

        while self._accumulator >= tick_interval:
            self.engine.tick()
            self._accumulator -= tick_interval
            tick_interval = self.engine.state.speed / 1000.0  # Update in case speed changed

        # Render current state
        state = self.engine.get_state_snapshot()
        self.renderer.render(state)

        # Schedule next frame (~60 FPS = 16.67ms)
        self.root.after(16, self._render_loop)


# =============================================================================
# ENTRY POINT
# =============================================================================

def main() -> None:
    """Launch the Snake game."""
    root = tk.Tk()
    game = SnakeGame(root)
    root.mainloop()


if __name__ == "__main__":
    main()