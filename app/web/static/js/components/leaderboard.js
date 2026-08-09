"use strict";

(() => {
  const pageSize = 10;
  const pollIntervalMs = 15000;
  const refreshDebounceMs = 150;

  function leaderboard() {
    return {
      player: window.ArcadeProof.playerStore.current(),
      gameKey: "",
      periodStart: "",
      items: [],
      personalEntry: null,
      cursor: 0,
      nextCursor: null,
      loading: false,
      error: "",
      initialized: false,
      starting: false,
      liveStatus: "connecting",
      eventSource: null,
      pollTimer: null,
      refreshTimer: null,
      refreshPending: false,
      requestGeneration: 0,
      connectedOnce: false,
      playerListener: null,
      onlineListener: null,
      visibilityListener: null,

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
        if (this.starting || this.initialized) return;
        this.starting = true;
        this.gameKey = this.$el.dataset.gameKey || "skill_check";
        this.periodStart = this.$el.dataset.periodStart;
        this.playerListener = (event) => {
          this.requestGeneration += 1;
          this.player = event.detail;
          this.cursor = 0;
          if (this.initialized) this.scheduleRefresh();
        };
        this.onlineListener = () => this.scheduleRefresh();
        this.visibilityListener = () => {
          if (document.visibilityState === "visible") this.scheduleRefresh();
        };
        window.addEventListener("arcade-proof:player", this.playerListener);
        window.addEventListener("online", this.onlineListener);
        document.addEventListener("visibilitychange", this.visibilityListener);
        this.connectEventStream();
        this.pollTimer = window.setInterval(() => this.scheduleRefresh(), pollIntervalMs);
        try {
          this.player = await window.ArcadeProof.playerStore.validateCurrent();
        } catch (error) {
          this.player = window.ArcadeProof.playerStore.current();
          if (error.code !== "not_found" && error.code !== "forbidden") {
            this.error = error.message || "The local demo player could not be validated.";
            this.initialized = true;
            this.starting = false;
            return;
          }
        }
        this.initialized = true;
        this.starting = false;
        await this.load();
      },

      destroy() {
        this.eventSource?.close();
        if (this.pollTimer) window.clearInterval(this.pollTimer);
        if (this.refreshTimer) window.clearTimeout(this.refreshTimer);
        if (this.playerListener) window.removeEventListener("arcade-proof:player", this.playerListener);
        if (this.onlineListener) window.removeEventListener("online", this.onlineListener);
        if (this.visibilityListener) {
          document.removeEventListener("visibilitychange", this.visibilityListener);
        }
        this.requestGeneration += 1;
      },

      connectEventStream() {
        this.eventSource?.close();
        this.liveStatus = this.connectedOnce ? "reconnecting" : "connecting";
        this.eventSource = new EventSource(
          window.ArcadeProof.api.leaderboardEventsUrl(this.gameKey, this.periodStart),
        );
        this.eventSource.onopen = () => {
          this.liveStatus = "live";
          if (this.connectedOnce) this.scheduleRefresh();
          this.connectedOnce = true;
        };
        this.eventSource.onerror = () => {
          this.liveStatus = "reconnecting";
        };
        this.eventSource.addEventListener("leaderboard-change", () => {
          this.liveStatus = "updated";
          this.scheduleRefresh();
        });
      },

      scheduleRefresh() {
        if (this.refreshTimer) window.clearTimeout(this.refreshTimer);
        this.refreshTimer = window.setTimeout(() => {
          this.refreshTimer = null;
          this.load();
        }, refreshDebounceMs);
      },

      async load() {
        if (this.loading) {
          this.refreshPending = true;
          return;
        }
        const generation = this.requestGeneration;
        const playerId = this.player?.id;
        const cursor = this.cursor;
        this.error = "";
        if (!playerId) {
          this.items = [];
          this.personalEntry = null;
          this.nextCursor = null;
          return;
        }
        this.loading = true;
        try {
          const page = await window.ArcadeProof.api.getLeaderboard(
            playerId,
            this.gameKey,
            this.periodStart,
            cursor,
            pageSize,
          );
          let personalEntry = null;
          try {
            const rank = await window.ArcadeProof.api.getPlayerRank(
              playerId,
              this.gameKey,
              this.periodStart,
            );
            personalEntry = rank.entry;
          } catch (error) {
            if (error.code !== "leaderboard_entry_not_found") throw error;
          }
          if (generation !== this.requestGeneration || playerId !== this.player?.id) return;
          this.items = page.items;
          this.nextCursor = page.next_cursor;
          this.personalEntry = personalEntry;
          if (this.liveStatus === "updated") {
            window.setTimeout(() => {
              if (this.liveStatus === "updated") this.liveStatus = "live";
            }, 1200);
          }
        } catch (error) {
          if (generation !== this.requestGeneration || playerId !== this.player?.id) return;
          this.error = error.message || "The weekly scoreboard could not be loaded.";
        } finally {
          this.loading = false;
          if (this.refreshPending) {
            this.refreshPending = false;
            this.scheduleRefresh();
          }
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
