"""Pong renderer for Pi's 240x280 ST7789 LCD using PIL.

Reuses the TamagotchiDisplay's image/draw/board objects.
Called from the main loop when app_state == "GAME_PONG".
"""


class PongRenderer:
    # Layout constants
    PADDLE_W = 6
    PADDLE_H = 40
    PADDLE_MARGIN = 8
    BALL_SIZE = 6

    # Colors
    COLOR_BG = (0, 0, 0)
    COLOR_FG = (255, 255, 255)
    COLOR_DIM = (40, 40, 40)
    COLOR_SCORE = (0, 200, 200)
    COLOR_GAMEOVER = (255, 180, 0)

    def __init__(self, display):
        """display: TamagotchiDisplay instance."""
        self.display = display
        self.W = display.W  # 240
        self.H = display.H  # 280

    def render(self, state, ai_mode="rule"):
        """Render one Pong frame to the display's PIL image."""
        draw = self.display.draw

        # Black background
        draw.rectangle([0, 0, self.W, self.H], fill=self.COLOR_BG)

        # Center dashed line
        for y in range(0, self.H, 14):
            draw.rectangle([self.W // 2 - 1, y, self.W // 2, y + 7],
                           fill=self.COLOR_DIM)

        # Scores
        font = self.display.font_lg if hasattr(self.display, "font_lg") else None
        s1 = str(state["score1"])
        s2 = str(state["score2"])
        if font:
            draw.text((self.W // 2 - 40, 8), s1, fill=self.COLOR_SCORE, font=font)
            draw.text((self.W // 2 + 28, 8), s2, fill=self.COLOR_SCORE, font=font)
        else:
            draw.text((self.W // 2 - 30, 8), s1, fill=self.COLOR_SCORE)
            draw.text((self.W // 2 + 22, 8), s2, fill=self.COLOR_SCORE)

        # Paddle 1 (left, player)
        p1_x = self.PADDLE_MARGIN
        p1_top = int(state["paddle1_y"] - self.PADDLE_H / 2)
        draw.rectangle([p1_x, p1_top,
                        p1_x + self.PADDLE_W, p1_top + self.PADDLE_H],
                       fill=self.COLOR_FG)

        # Paddle 2 (right, AI)
        p2_x = self.W - self.PADDLE_MARGIN - self.PADDLE_W
        p2_top = int(state["paddle2_y"] - self.PADDLE_H / 2)
        draw.rectangle([p2_x, p2_top,
                        p2_x + self.PADDLE_W, p2_top + self.PADDLE_H],
                       fill=self.COLOR_FG)

        # Ball
        bx = int(state["ball_x"])
        by = int(state["ball_y"])
        half = self.BALL_SIZE // 2
        draw.rectangle([bx - half, by - half, bx + half, by + half],
                       fill=self.COLOR_FG)

        # AI mode label (bottom right)
        font_sm = self.display.font_sm if hasattr(self.display, "font_sm") else None
        label = f"AI: {ai_mode}"
        if font_sm:
            draw.text((self.W - 70, self.H - 16), label,
                      fill=self.COLOR_DIM, font=font_sm)
        else:
            draw.text((self.W - 60, self.H - 12), label, fill=self.COLOR_DIM)

        # Controls hint (bottom left)
        hint = "[Start] Exit"
        if font_sm:
            draw.text((4, self.H - 16), hint, fill=self.COLOR_DIM, font=font_sm)
        else:
            draw.text((4, self.H - 12), hint, fill=self.COLOR_DIM)

        # Game over overlay
        if state["game_over"]:
            winner = "You win!" if state["winner"] == 1 else "AI wins!"
            if font:
                bbox = font.getbbox(winner)
                tw = bbox[2] - bbox[0] if bbox else 80
                draw.text(((self.W - tw) // 2, self.H // 2 - 20), winner,
                          fill=self.COLOR_GAMEOVER, font=font)
            else:
                draw.text((self.W // 2 - 30, self.H // 2 - 10), winner,
                          fill=self.COLOR_GAMEOVER)

            restart_text = "[A] Restart"
            if font_sm:
                bbox = font_sm.getbbox(restart_text)
                tw = bbox[2] - bbox[0] if bbox else 60
                draw.text(((self.W - tw) // 2, self.H // 2 + 10), restart_text,
                          fill=self.COLOR_FG, font=font_sm)
            else:
                draw.text((self.W // 2 - 30, self.H // 2 + 10), restart_text,
                          fill=self.COLOR_FG)

        # Flush to LCD
        self.display._flush()
