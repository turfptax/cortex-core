"""Pong game engine — pure Python, no dependencies.

Frame-rate independent physics via tick(dt). Coordinate system matches
the Pi's 240x280 LCD: (0,0) is top-left, y increases downward.
"""

import math
import random


class PongGame:
    # Field dimensions (match Pi LCD)
    WIDTH = 240
    HEIGHT = 280

    # Paddle
    PADDLE_W = 6
    PADDLE_H = 40
    PADDLE_MARGIN = 8
    PADDLE_SPEED = 200  # pixels/sec

    # Ball
    BALL_SIZE = 6
    BALL_SPEED_INITIAL = 120  # pixels/sec
    BALL_SPEED_MAX = 220
    BALL_SPEED_INCREMENT = 8  # per paddle hit

    WIN_SCORE = 5

    def __init__(self):
        self.paddle1_y = 0.0
        self.paddle2_y = 0.0
        self.ball_x = 0.0
        self.ball_y = 0.0
        self.ball_vx = 0.0
        self.ball_vy = 0.0
        self.score1 = 0
        self.score2 = 0
        self.game_over = False
        self.winner = None
        self._ball_speed = self.BALL_SPEED_INITIAL
        self._serve_pause = 0.0
        self.reset()

    def reset(self):
        self.paddle1_y = self.HEIGHT / 2
        self.paddle2_y = self.HEIGHT / 2
        self.score1 = 0
        self.score2 = 0
        self.game_over = False
        self.winner = None
        self._reset_ball(direction=1)

    def _reset_ball(self, direction=1):
        self.ball_x = self.WIDTH / 2
        self.ball_y = self.HEIGHT / 2
        angle = random.uniform(-0.5, 0.5)
        self.ball_vx = self.BALL_SPEED_INITIAL * math.cos(angle) * direction
        self.ball_vy = self.BALL_SPEED_INITIAL * math.sin(angle)
        self._ball_speed = self.BALL_SPEED_INITIAL
        self._serve_pause = 0.5

    def tick(self, dt):
        """Advance physics. Returns event dict or None."""
        if self.game_over:
            return None

        if self._serve_pause > 0:
            self._serve_pause -= dt
            return None

        # Move ball
        self.ball_x += self.ball_vx * dt
        self.ball_y += self.ball_vy * dt

        half = self.BALL_SIZE / 2

        # Wall bounce (top/bottom)
        if self.ball_y - half <= 0:
            self.ball_y = half
            self.ball_vy = abs(self.ball_vy)
        elif self.ball_y + half >= self.HEIGHT:
            self.ball_y = self.HEIGHT - half
            self.ball_vy = -abs(self.ball_vy)

        # Paddle 1 (left) collision
        p1_x = self.PADDLE_MARGIN + self.PADDLE_W
        p1_top = self.paddle1_y - self.PADDLE_H / 2
        p1_bot = self.paddle1_y + self.PADDLE_H / 2
        if (self.ball_x - half <= p1_x and
                self.ball_vx < 0 and
                p1_top <= self.ball_y <= p1_bot):
            self.ball_x = p1_x + half
            self._bounce_off_paddle(self.paddle1_y)

        # Paddle 2 (right) collision
        p2_x = self.WIDTH - self.PADDLE_MARGIN - self.PADDLE_W
        p2_top = self.paddle2_y - self.PADDLE_H / 2
        p2_bot = self.paddle2_y + self.PADDLE_H / 2
        if (self.ball_x + half >= p2_x and
                self.ball_vx > 0 and
                p2_top <= self.ball_y <= p2_bot):
            self.ball_x = p2_x - half
            self._bounce_off_paddle(self.paddle2_y)

        # Scoring
        event = None
        if self.ball_x < 0:
            self.score2 += 1
            event = {"scored": 2}
            if self.score2 >= self.WIN_SCORE:
                self.game_over = True
                self.winner = 2
            else:
                self._reset_ball(direction=1)
        elif self.ball_x > self.WIDTH:
            self.score1 += 1
            event = {"scored": 1}
            if self.score1 >= self.WIN_SCORE:
                self.game_over = True
                self.winner = 1
            else:
                self._reset_ball(direction=-1)

        return event

    def _bounce_off_paddle(self, paddle_y):
        offset = (self.ball_y - paddle_y) / (self.PADDLE_H / 2)
        offset = max(-1.0, min(1.0, offset))

        self._ball_speed = min(self._ball_speed + self.BALL_SPEED_INCREMENT,
                               self.BALL_SPEED_MAX)

        angle = offset * math.pi / 4  # max 45 degrees
        direction = 1 if self.ball_vx < 0 else -1
        self.ball_vx = self._ball_speed * math.cos(angle) * direction
        self.ball_vy = self._ball_speed * math.sin(angle)

    def move_paddle(self, player, direction, dt):
        """Move paddle. player: 1 or 2, direction: -1/0/1, dt: seconds."""
        delta = direction * self.PADDLE_SPEED * dt
        half_h = self.PADDLE_H / 2
        if player == 1:
            self.paddle1_y = max(half_h, min(self.HEIGHT - half_h,
                                             self.paddle1_y + delta))
        else:
            self.paddle2_y = max(half_h, min(self.HEIGHT - half_h,
                                             self.paddle2_y + delta))

    def get_state(self):
        """Full game state dict for rendering."""
        return {
            "ball_x": self.ball_x,
            "ball_y": self.ball_y,
            "ball_vx": self.ball_vx,
            "ball_vy": self.ball_vy,
            "paddle1_y": self.paddle1_y,
            "paddle2_y": self.paddle2_y,
            "score1": self.score1,
            "score2": self.score2,
            "game_over": self.game_over,
            "winner": self.winner,
            "serving": self._serve_pause > 0,
        }

    def get_ai_observation(self):
        """Discretized state tuple for Q-learning.

        Returns (ball_x_bin, ball_y_bin, ball_vy_sign, paddle2_y_bin).
        State space: 12 * 10 * 3 * 10 = 3,600 states.
        """
        bx = max(0, min(11, int(self.ball_x / self.WIDTH * 12)))
        by = max(0, min(9, int(self.ball_y / self.HEIGHT * 10)))
        vy_sign = 0 if self.ball_vy < -20 else (2 if self.ball_vy > 20 else 1)
        py = max(0, min(9, int(self.paddle2_y / self.HEIGHT * 10)))
        return (bx, by, vy_sign, py)
