"use strict";

(() => {
  const pageSize = 10;
  const pollIntervalMs = 15000;
  const refreshDebounceMs = 150;
  const rankMotionMs = 520;
  const highlightMotionMs = 640;
  const weekMs = 7 * 24 * 60 * 60 * 1000;

  function currentUtcPeriod(now = new Date()) {
    const daysSinceMonday = (now.getUTCDay() + 6) % 7;
    const start = new Date(Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate() - daysSinceMonday,
    ));
    return { start: start.toISOString(), end: new Date(start.getTime() + weekMs).toISOString() };
  }

  function leaderboard() {
    return {
      player: window.ArcadeProof.playerStore.current(),
      gameKey: "",
      periodStart: "",
      periodEnd: "",
      periodCountdown: "Calculating…",
      items: [],
      personalEntry: null,
      loading: false,
      hasLoaded: false,
      error: "",
      announcement: "",
      initialized: false,
      starting: false,
      liveStatus: "connecting",
      eventSource: null,
      pollTimer: null,
      countdownTimer: null,
      refreshTimer: null,
      statusTimer: null,
      refreshPending: false,
      announcePending: false,
      requestGeneration: 0,
      connectedOnce: false,
      playerListener: null,
      onlineListener: null,
      visibilityListener: null,

      get hasPlayer() {
        return Boolean(this.player);
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

      get liveStatusLabel() {
        if (this.liveStatus === "reconnecting") return "Reconnecting";
        if (this.liveStatus === "updated") return "Updated";
        if (this.liveStatus === "new-week") return "New week";
        return this.liveStatus === "connecting" ? "Connecting" : "Live";
      },

      get weekLabel() {
        const start = new Date(this.periodStart);
        const end = new Date(this.periodEnd);
        const dayMonth = new Intl.DateTimeFormat(undefined, {
          day: "2-digit",
          month: "short",
          timeZone: "UTC",
        });
        const endLabel = new Intl.DateTimeFormat(undefined, {
          day: "2-digit",
          month: "short",
          year: "numeric",
          timeZone: "UTC",
        });
        return `${dayMonth.format(start)}–${endLabel.format(end)}`;
      },

      async init() {
        if (this.starting || this.initialized) return;
        this.starting = true;
        this.gameKey = this.$el.dataset.gameKey;
        this.periodStart = this.$el.dataset.periodStart;
        this.periodEnd = this.$el.dataset.periodEnd;
        this.playerListener = (event) => {
          this.requestGeneration += 1;
          this.player = event.detail;
          this.items = [];
          this.personalEntry = null;
          this.hasLoaded = false;
          this.error = "";
          if (this.initialized) this.scheduleRefresh();
        };
        this.onlineListener = () => this.scheduleRefresh();
        this.visibilityListener = () => {
          if (document.visibilityState === "visible") {
            this.updatePeriodCountdown();
            this.scheduleRefresh();
          }
        };
        window.addEventListener("arcade-proof:player", this.playerListener);
        window.addEventListener("online", this.onlineListener);
        document.addEventListener("visibilitychange", this.visibilityListener);
        this.connectEventStream();
        this.updatePeriodCountdown();
        this.countdownTimer = window.setInterval(() => this.updatePeriodCountdown(), 1000);
        this.pollTimer = window.setInterval(() => this.scheduleRefresh(), pollIntervalMs);
        try {
          this.player = await window.ArcadeProof.playerStore.validateCurrent();
        } catch (error) {
          this.player = window.ArcadeProof.playerStore.current();
          if (error.code !== "not_found" && error.code !== "forbidden") {
            this.error = error.message || "Your local player could not be checked.";
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
        if (this.countdownTimer) window.clearInterval(this.countdownTimer);
        if (this.refreshTimer) window.clearTimeout(this.refreshTimer);
        if (this.statusTimer) window.clearTimeout(this.statusTimer);
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
          this.announcePending = true;
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

      updatePeriodCountdown() {
        const now = new Date();
        let remainingMs = new Date(this.periodEnd).getTime() - now.getTime();
        if (remainingMs <= 0) {
          this.startCurrentPeriod(now);
          remainingMs = new Date(this.periodEnd).getTime() - now.getTime();
        }
        const remainingSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
        const days = Math.floor(remainingSeconds / 86400);
        const hours = Math.floor((remainingSeconds % 86400) / 3600);
        const minutes = Math.floor((remainingSeconds % 3600) / 60);
        const seconds = remainingSeconds % 60;
        if (days) this.periodCountdown = `${days}d ${hours}h`;
        else if (hours) this.periodCountdown = `${hours}h ${minutes}m`;
        else this.periodCountdown = `${minutes}m ${seconds}s`;
      },

      startCurrentPeriod(now) {
        const period = currentUtcPeriod(now);
        if (period.start === this.periodStart) return;
        this.requestGeneration += 1;
        this.periodStart = period.start;
        this.periodEnd = period.end;
        this.$el.dataset.periodStart = period.start;
        this.$el.dataset.periodEnd = period.end;
        this.items = [];
        this.personalEntry = null;
        this.hasLoaded = false;
        this.liveStatus = "new-week";
        this.announcement = "A new UTC leaderboard week has started.";
        this.connectedOnce = false;
        this.connectEventStream();
        this.liveStatus = "new-week";
        this.scheduleRefresh();
      },

      snapshotRowPositions() {
        const positions = new Map();
        this.$el.querySelectorAll("[data-leaderboard-row]").forEach((row) => {
          positions.set(row.dataset.scoreId, row.getBoundingClientRect());
        });
        return positions;
      },

      describeChange(previousItems, nextItems) {
        const previousById = new Map(previousItems.map((entry) => [entry.score_id, entry]));
        const inserted = nextItems.find((entry) => !previousById.has(entry.score_id));
        if (inserted) {
          return `${inserted.public_label} entered at number ${inserted.rank} with ${inserted.final_score} points.`;
        }
        const moved = nextItems.find((entry) => previousById.get(entry.score_id)?.rank !== entry.rank);
        if (moved) {
          return `${moved.public_label} moved to number ${moved.rank} with ${moved.final_score} points.`;
        }
        return "Leaderboard updated.";
      },

      animateChanges(previousItems, previousPositions) {
        const previousById = new Map(previousItems.map((entry) => [entry.score_id, entry]));
        const reducedMotion = window.ArcadeProof.preferences.reducedMotion();
        this.$el.querySelectorAll("[data-leaderboard-row]").forEach((row) => {
          if (typeof row.animate !== "function") return;
          const scoreId = row.dataset.scoreId;
          const oldPosition = previousPositions.get(scoreId);
          const entry = this.items.find((candidate) => candidate.score_id === scoreId);
          const isInserted = !previousById.has(scoreId);
          const moved = oldPosition && Math.abs(oldPosition.top - row.getBoundingClientRect().top) > 1;
          if (!isInserted && !moved) return;
          if (reducedMotion) {
            row.animate(
              [
                { outlineColor: "rgba(103, 241, 207, 0.9)", backgroundColor: "rgba(103, 241, 207, 0.16)" },
                { outlineColor: "rgba(103, 241, 207, 0)", backgroundColor: "rgba(255, 255, 255, 0.03)" },
              ],
              { duration: 320, easing: "ease-out" },
            );
            return;
          }
          if (isInserted) {
            row.animate(
              [
                { opacity: 0.72, transform: "scale(0.98)", boxShadow: "0 0 0 rgba(255, 191, 90, 0)" },
                {
                  opacity: 1,
                  transform: "scale(1)",
                  boxShadow: "0 0 0.7rem rgba(255, 191, 90, 0.38), 0 0 1.4rem rgba(103, 241, 207, 0.42)",
                },
                { opacity: 1, transform: "scale(1)", boxShadow: "0 0 0 rgba(103, 241, 207, 0)" },
              ],
              { duration: highlightMotionMs, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" },
            );
          } else if (oldPosition) {
            const distance = oldPosition.top - row.getBoundingClientRect().top;
            row.animate(
              [{ transform: `translateY(${distance}px)` }, { transform: "translateY(0)" }],
              { duration: rankMotionMs, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" },
            );
          }
          if (entry && (isInserted || previousById.get(scoreId)?.rank !== entry.rank)) {
            row.querySelector("[data-leaderboard-score]")?.animate(
              [{ color: "#ffbf5a", transform: "scale(1.08)" }, { color: "currentColor", transform: "scale(1)" }],
              { duration: 420, easing: "ease-out" },
            );
          }
        });
      },

      async load() {
        if (this.loading) {
          this.refreshPending = true;
          return;
        }
        const generation = this.requestGeneration;
        const playerId = this.player?.id;
        const previousItems = this.items.map((entry) => ({ ...entry }));
        const previousPositions = this.snapshotRowPositions();
        const wasLoaded = this.hasLoaded;
        const shouldAnnounce = this.announcePending;
        this.announcePending = false;
        this.error = "";
        if (!playerId) {
          this.items = [];
          this.personalEntry = null;
          this.hasLoaded = false;
          return;
        }
        this.loading = true;
        try {
          const page = await window.ArcadeProof.api.getLeaderboard(
            playerId,
            this.gameKey,
            this.periodStart,
            0,
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
          this.personalEntry = personalEntry;
          this.hasLoaded = true;
          await this.$nextTick();
          if (wasLoaded) {
            const visibleChanged = previousItems.length !== this.items.length
              || this.items.some((entry) => {
                const previous = previousItems.find((candidate) => candidate.score_id === entry.score_id);
                return !previous || previous.rank !== entry.rank || previous.final_score !== entry.final_score;
              });
            if (visibleChanged) this.animateChanges(previousItems, previousPositions);
            if (visibleChanged || shouldAnnounce) {
              this.announcement = this.describeChange(previousItems, this.items);
            }
          }
          if (this.liveStatus === "updated") {
            if (this.statusTimer) window.clearTimeout(this.statusTimer);
            this.statusTimer = window.setTimeout(() => {
              if (this.liveStatus === "updated") this.liveStatus = "live";
            }, 1200);
          }
        } catch (error) {
          if (generation !== this.requestGeneration || playerId !== this.player?.id) return;
          this.error = error.message || "This week’s scores could not be loaded.";
        } finally {
          this.loading = false;
          if (this.refreshPending) {
            this.refreshPending = false;
            this.scheduleRefresh();
          }
        }
      },

      openIdentity() {
        window.ArcadeProof.dialogTrigger = document.activeElement;
        const dialog = document.getElementById("identity-dialog");
        if (dialog && !dialog.open) dialog.showModal();
      },
    };
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.components = window.ArcadeProof.components || {};
  window.ArcadeProof.components.leaderboard = leaderboard;
})();
