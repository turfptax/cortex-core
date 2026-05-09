"""Pong AI opponent — rule-based fallback + Q-table model.

The rule-based AI tracks the ball with configurable difficulty.
After Q-learning training, loads a Q-table JSON for smarter play.
"""

import json
import os
import random


class PongAI:
    def __init__(self, model_path=None, difficulty=0.7):
        self.mode = "rule"
        self.difficulty = difficulty  # 0.0 = never moves, 1.0 = perfect
        self.q_table = {}
        if model_path and os.path.exists(model_path):
            self.load_model(model_path)

    def load_model(self, path):
        """Load trained Q-table from JSON."""
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if data.get("type") != "qtable":
                return
            self.q_table = {}
            for key_str, values in data.get("table", {}).items():
                key = tuple(int(k) for k in key_str.split(","))
                self.q_table[key] = values
            self.mode = "qtable"
            print(f"Pong AI: loaded Q-table ({len(self.q_table)} states)")
        except Exception as e:
            print(f"Pong AI: failed to load model: {e}")

    def get_action(self, game_state):
        """Return -1 (up), 0 (stay), or 1 (down) for paddle 2."""
        if self.mode == "qtable" and self.q_table:
            return self._q_action(game_state)
        return self._rule_action(game_state)

    def _rule_action(self, state):
        """Track ball y with imperfection."""
        # Only move when ball heading toward AI paddle
        if state["ball_vx"] <= 0:
            return 0

        diff = state["ball_y"] - state["paddle2_y"]

        # Dead zone
        if abs(diff) < 10:
            return 0

        # Random imprecision
        if random.random() > self.difficulty:
            return 0

        return 1 if diff > 0 else -1

    def _q_action(self, state):
        """Q-table lookup."""
        from games.pong import PongGame

        # Discretize state the same way as PongGame.get_ai_observation()
        bx = max(0, min(11, int(state["ball_x"] / PongGame.WIDTH * 12)))
        by = max(0, min(9, int(state["ball_y"] / PongGame.HEIGHT * 10)))
        vy = state["ball_vy"]
        vy_sign = 0 if vy < -20 else (2 if vy > 20 else 1)
        py = max(0, min(9, int(state["paddle2_y"] / PongGame.HEIGHT * 10)))

        obs = (bx, by, vy_sign, py)
        q_values = self.q_table.get(obs)
        if q_values is None:
            return self._rule_action(state)  # fallback for unseen states

        best_action = q_values.index(max(q_values))  # 0=up, 1=stay, 2=down
        return best_action - 1  # map to -1, 0, 1
