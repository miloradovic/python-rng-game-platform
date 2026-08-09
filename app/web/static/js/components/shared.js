"use strict";

(() => {
  function notify(message, kind = "info") {
    window.dispatchEvent(new CustomEvent("arcade-proof:toast", { detail: { message, kind } }));
    window.ArcadeProof.preferences.playCue(kind);
  }

  function closeDialog(id) {
    document.getElementById(id)?.close();
    const trigger = window.ArcadeProof.dialogTrigger;
    window.ArcadeProof.dialogTrigger = null;
    if (trigger instanceof HTMLElement) window.setTimeout(() => trigger.focus(), 0);
  }

  function identityManager() {
    return {
      players: [],
      displayName: "",
      error: "",
      busy: false,
      limitedStorage: !window.ArcadeProof.playerStore.persistent,
      get hasPlayers() { return this.players.length > 0; },
      get hasError() { return Boolean(this.error); },
      refresh() { this.players = window.ArcadeProof.playerStore.snapshot().players; },
      async initialize() {
        this.refresh();
        try {
          await window.ArcadeProof.playerStore.validateCurrent();
          this.refresh();
        } catch (error) {
          this.error = error.message;
        }
        if (!window.ArcadeProof.playerStore.current()) window.setTimeout(() => this.open(), 0);
      },
      open() {
        const dialog = document.getElementById("identity-dialog");
        if (dialog && !dialog.open) dialog.showModal();
      },
      close() { closeDialog("identity-dialog"); },
      async create() {
        const name = this.displayName.trim();
        if (name.length < 2) {
          this.error = "Enter a display name with at least 2 characters.";
          return;
        }
        this.busy = true;
        this.error = "";
        try {
          const player = await window.ArcadeProof.api.createPlayer(name);
          window.ArcadeProof.playerStore.save(player);
          this.displayName = "";
          this.refresh();
          this.close();
          notify(`Playing locally as ${player.display_name}.`, "success");
        } catch (error) {
          this.error = error.code === "http_422"
            ? "Use 2–50 letters, numbers, spaces, apostrophes, periods, or hyphens."
            : error.message;
        } finally {
          this.busy = false;
        }
      },
      select(event) {
        const player = window.ArcadeProof.playerStore.select(event.currentTarget.dataset.playerId);
        if (player) {
          this.close();
          notify(`Switched to ${player.display_name}.`, "success");
        }
      },
      clear() {
        window.ArcadeProof.playerStore.clear();
        window.ArcadeProof.journal.clear();
        this.refresh();
        this.error = "Create or restore a local demo player to continue.";
      },
    };
  }

  function preferenceManager() {
    return {
      reduceMotion: false,
      sound: false,
      status: "Sounds are off by default.",
      limitedStorage: !window.ArcadeProof.preferences.persistent,
      initialize() {
        const settings = window.ArcadeProof.preferences.snapshot();
        this.reduceMotion = settings.reduceMotion;
        this.sound = settings.sound;
        this.status = window.ArcadeProof.preferences.reducedMotion()
          ? "Reduced motion is active."
          : "Standard motion is active; sounds are off unless enabled.";
      },
      save() {
        window.ArcadeProof.preferences.save({
          reduceMotion: this.reduceMotion,
          sound: this.sound,
        });
      },
      setMotion(event) {
        this.reduceMotion = event.currentTarget.checked;
        this.save();
        this.status = window.ArcadeProof.preferences.reducedMotion()
          ? "Reduced motion is active."
          : "Standard motion is active.";
      },
      setSound(event) {
        this.sound = event.currentTarget.checked;
        this.save();
        this.status = this.sound
          ? "Supplementary sounds are enabled."
          : "Sounds are off. Results remain available as text.";
        if (this.sound) window.ArcadeProof.preferences.playCue("success");
      },
      close() { closeDialog("preferences-dialog"); },
    };
  }

  function networkStatus() {
    return {
      offline: !navigator.onLine,
      initialize() {
        window.addEventListener("online", () => {
          this.offline = false;
          notify("Back online. Refreshing server state is now available.", "success");
        });
        window.addEventListener("offline", () => { this.offline = true; });
      },
    };
  }

  function toastRegion() {
    return {
      toasts: [],
      initialize() {
        window.addEventListener("arcade-proof:toast", (event) => {
          const toast = { id: crypto.randomUUID(), ...event.detail };
          this.toasts.push(toast);
          window.setTimeout(() => {
            this.toasts = this.toasts.filter((candidate) => candidate.id !== toast.id);
          }, 5000);
        });
      },
    };
  }

  function operationRecovery() {
    return {
      gameKey: "",
      state: null,
      message: "Choose a player to check your saved round.",
      busy: false,
      countdownLabel: "",
      timer: null,
      get player() { return window.ArcadeProof.playerStore.current(); },
      get hasSession() { return Boolean(this.state?.session); },
      get sessionStatus() { return this.state?.session?.status || "None"; },
      get resultLabel() {
        const result = this.state?.session?.outcome?.result;
        if (!result) return "No result recorded";
        if (result.score !== undefined) return `${result.score} points`;
        if (result.reward_key) return String(result.reward_key).replaceAll("_", " ");
        if (result.choice) return String(result.choice);
        return "Result recorded";
      },
      get rewardLabel() {
        const reward = this.state?.session?.reward;
        return reward ? `${reward.value} points · ${reward.status}` : "No reward recorded";
      },
      get hasCountdown() { return Boolean(this.state?.next_play_at); },
      get canCancel() { return this.state?.session?.status === "active"; },
      get hasJournal() { return Boolean(this.player && window.ArcadeProof.journal.get(this.player.id, this.gameKey)); },
      initialize() {
        this.gameKey = this.$el.dataset.gameKey;
        window.addEventListener("arcade-proof:player", () => this.recover());
        this.recover();
      },
      updateCountdown() {
        window.clearInterval(this.timer);
        if (!this.state?.next_play_at) {
          this.countdownLabel = "";
          return;
        }
        const clock = new window.ArcadeProof.ServerClock(this.state.server_time);
        const render = () => { this.countdownLabel = clock.label(this.state.next_play_at); };
        render();
        this.timer = window.setInterval(render, 1000);
      },
      async recover() {
        const player = this.player;
        if (!player) {
          this.state = null;
          this.message = "Choose a player to check your saved round.";
          return;
        }
        this.busy = true;
        this.message = "Checking your latest round…";
        try {
          this.state = await window.ArcadeProof.api.getGameState(player.id, this.gameKey);
          if (!this.state.session) this.message = "No previous play is recorded for this game.";
          else if (this.state.session.status === "active") this.message = "Your unfinished round can be resumed here or cancelled.";
          else if (this.state.session.status === "expired") this.message = "The latest play expired before completion. Start again when available.";
          else this.message = "Your latest result is ready again.";
          this.updateCountdown();
        } catch (error) {
          this.message = error.message;
        } finally {
          this.busy = false;
        }
      },
      async cancel() {
        if (!this.player || !this.state?.session) return;
        this.busy = true;
        try {
          await window.ArcadeProof.api.cancelSession(this.player.id, this.state.session.id);
          window.ArcadeProof.journal.discard(this.player.id, this.gameKey);
          notify("Round cancelled. Its fairness record is still available.", "success");
          await this.recover();
        } catch (error) {
          this.message = error.message;
        } finally {
          this.busy = false;
        }
      },
      discardJournal() {
        if (!this.player) return;
        window.ArcadeProof.journal.discard(this.player.id, this.gameKey);
        notify("Saved browser step cleared. Your finished results were not changed.", "info");
      },
    };
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.components = {
    identityManager,
    networkStatus,
    operationRecovery,
    preferenceManager,
    toastRegion,
  };
  window.ArcadeProof.notify = notify;
})();
