"use strict";

(() => {
  const pageSize = 10;
  const gameKey = "skill_check";

  function leaderboard() {
    return {
      player: window.ArcadeProof.playerStore.current(),
      periodStart: "",
      items: [],
      personalEntry: null,
      cursor: 0,
      nextCursor: null,
      loading: false,
      error: "",

      get hasPlayer() {
        return Boolean(this.player);
      },

      get topEntries() {
        return this.cursor === 0 ? this.items.filter((entry) => entry.rank <= 3) : [];
      },

      get listEntries() {
        return this.cursor === 0 ? this.items.filter((entry) => entry.rank > 3) : this.items;
      },

      get hasPrevious() {
        return this.cursor > 0;
      },

      get hasNext() {
        return this.nextCursor !== null;
      },

      get pageNumber() {
        return Math.floor(this.cursor / pageSize) + 1;
      },

      get personalLabel() {
        return this.personalEntry ? this.personalEntry.public_label : "";
      },

      get personalRank() {
        return this.personalEntry ? this.personalEntry.rank : "";
      },

      get personalScore() {
        return this.personalEntry ? this.personalEntry.final_score : "";
      },

      async init() {
        this.periodStart = this.$el.dataset.periodStart;
        window.addEventListener("arcade-proof:player", (event) => {
          this.player = event.detail;
          this.cursor = 0;
          this.load();
        });
        await this.load();
      },

      async load() {
        this.error = "";
        this.items = [];
        this.personalEntry = null;
        this.nextCursor = null;
        if (!this.player) return;
        this.loading = true;
        try {
          const page = await window.ArcadeProof.api.getLeaderboard(
            this.player.id,
            gameKey,
            this.periodStart,
            this.cursor,
            pageSize,
          );
          this.items = page.items;
          this.nextCursor = page.next_cursor;
          try {
            const rank = await window.ArcadeProof.api.getPlayerRank(
              this.player.id,
              gameKey,
              this.periodStart,
            );
            this.personalEntry = rank.entry;
          } catch (error) {
            if (error.code !== "leaderboard_entry_not_found") throw error;
          }
        } catch (error) {
          this.error = error.message || "The weekly scoreboard could not be loaded.";
        } finally {
          this.loading = false;
        }
      },

      async nextPage() {
        if (!this.nextCursor) return;
        this.cursor = Number(this.nextCursor);
        await this.load();
        this.focusBoard();
      },

      async previousPage() {
        this.cursor = Math.max(0, this.cursor - pageSize);
        await this.load();
        this.focusBoard();
      },

      focusBoard() {
        document.getElementById("scoreboard-heading")?.focus();
      },

      formatTime(value) {
        return new Intl.DateTimeFormat(undefined, {
          hour: "2-digit",
          minute: "2-digit",
          timeZone: "UTC",
          timeZoneName: "short",
        }).format(new Date(value));
      },
    };
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.components = window.ArcadeProof.components || {};
  window.ArcadeProof.components.leaderboard = leaderboard;
})();
