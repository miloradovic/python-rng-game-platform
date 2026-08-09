"use strict";

(() => {
  const gameKey = "skill_check";

  function skillCheck() {
    return {
      busy: false,
      status: "loading",
      phase: "intro",
      message: "Loading the server rules…",
      config: null,
      session: null,
      outcome: null,
      reward: null,
      finalScore: null,
      lockedSubmission: false,
      sequence: [],
      actions: [],
      digits: [],
      countdownLabel: "",
      timer: null,
      previewTimer: null,
      keyboardHandler: null,
      get player() { return window.ArcadeProof.playerStore.current(); },
      get canStart() {
        return !this.busy && ["ready", "result"].includes(this.status) && this.session?.status !== "active";
      },
      get isPreview() { return this.phase === "preview"; },
      get isInput() { return this.phase === "input"; },
      get isResult() { return Boolean(this.outcome); },
      get canSubmit() { return this.isInput && this.actions.length > 0 && !this.busy; },
      get undoDisabled() { return this.busy || this.lockedSubmission || this.actions.length === 0; },
      get canClaim() { return Boolean(this.reward && this.reward.status === "pending"); },
      get actionLabel() { return this.outcome ? "Start another challenge" : "Start challenge"; },
      get countdownPrefix() {
        return this.session?.status === "active" ? "Server window closes in" : "Next challenge in";
      },
      get enteredLabel() { return this.actions.length ? this.actions.join(" · ") : "No digits entered"; },
      get scoreLabel() { return this.outcome ? `${this.outcome.result.score} points` : ""; },
      get accuracyLabel() {
        if (!this.outcome) return "";
        return `${this.outcome.result.correct_actions} of ${this.sequence.length} in the correct prefix`;
      },
      get rewardLabel() { return this.reward ? `${this.reward.value} points · ${this.reward.status}` : ""; },
      async initialize() {
        this.refreshDigits();
        this.keyboardHandler = (event) => this.handleKey(event);
        window.addEventListener("keydown", this.keyboardHandler);
        window.addEventListener("arcade-proof:player", () => this.load());
        await this.load();
      },
      destroy() {
        window.removeEventListener("keydown", this.keyboardHandler);
        window.clearInterval(this.timer);
        window.clearTimeout(this.previewTimer);
      },
      refreshDigits() {
        this.digits = Array.from({ length: 10 }, (_, digit) => ({
          digit,
          label: `Digit ${digit}`,
          disabled: this.busy || this.lockedSubmission || this.actions.includes(digit),
          className: this.actions.includes(digit) ? "digit-key is-used" : "digit-key",
        }));
      },
      saveJournal(values) {
        if (!this.player) return;
        const current = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
        window.ArcadeProof.journal.save(this.player.id, gameKey, { ...current, ...values });
      },
      async load() {
        this.busy = true;
        try {
          this.config = await window.ArcadeProof.api.getGameConfig(gameKey);
          await this.recover();
        } catch (error) {
          this.status = "terminal_error";
          this.message = error.message;
        } finally {
          this.busy = false;
          this.refreshDigits();
        }
      },
      async recover() {
        window.clearInterval(this.timer);
        window.clearTimeout(this.previewTimer);
        this.countdownLabel = "";
        if (!this.player) {
          this.status = "ready";
          this.phase = "intro";
          this.message = "Choose a local demo player to start the memory challenge.";
          return;
        }
        this.status = "recovering";
        const state = await window.ArcadeProof.api.getGameState(this.player.id, gameKey);
        this.session = state.session;
        this.outcome = state.session?.outcome || null;
        this.reward = state.session?.reward || null;
        this.finalScore = state.session?.final_score || null;
        this.sequence = state.session?.challenge?.sequence || [];
        const countdownTarget = this.session?.status === "active"
          ? this.session.expires_at
          : state.next_play_at;
        if (countdownTarget) this.startCountdown(state.server_time, countdownTarget);
        if (!this.session) {
          this.status = state.next_play_at ? "cooldown" : "ready";
          this.phase = "intro";
          this.message = "The server will generate five unique digits. Score is based only on the correct prefix.";
          return;
        }
        this.saveJournal({ requestId: this.session.request_id, sessionId: this.session.id, outcome: this.outcome });
        if (this.session.status === "active") {
          this.status = "ready";
          const saved = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
          if (saved.pendingAction === "play" && Array.isArray(saved.actions)) {
            this.actions = [...saved.actions];
            this.lockedSubmission = true;
            this.phase = "input";
            this.message = "Your exact saved digit submission is ready to retry.";
            this.refreshDigits();
          } else {
            this.beginPreview();
          }
        } else if (this.session.status === "expired") {
          this.status = "expired";
          this.phase = "intro";
          this.message = "The server expiry passed. This challenge cannot accept more actions.";
        } else if (this.outcome) {
          this.phase = "result";
          this.status = state.next_play_at ? "cooldown" : "result";
          this.message = "The authoritative score was recovered. Browser timing did not change it.";
          if (!this.finalScore) await this.submitFinalScore();
        }
      },
      startCountdown(serverTime, target) {
        const clock = new window.ArcadeProof.ServerClock(serverTime);
        const render = () => {
          this.countdownLabel = clock.label(target);
          if (clock.remaining(target) === 0) {
            window.clearInterval(this.timer);
            window.setTimeout(() => this.recover(), 0);
          }
        };
        render();
        this.timer = window.setInterval(render, 1000);
      },
      async start() {
        if (!this.player) {
          document.getElementById("identity-dialog")?.showModal();
          return;
        }
        this.busy = true;
        this.status = "submitting";
        try {
          if (!this.session || this.session.status !== "active") {
            this.outcome = null;
            this.reward = null;
            this.finalScore = null;
            this.phase = "intro";
            this.lockedSubmission = false;
            const requestId = crypto.randomUUID();
            this.saveJournal({ requestId, pendingAction: "create_session" });
            this.session = await window.ArcadeProof.api.createSession(this.player.id, requestId, gameKey);
            this.sequence = this.session.challenge.sequence;
            this.saveJournal({ sessionId: this.session.id, pendingAction: "play" });
          }
          this.actions = [];
          this.status = "ready";
          this.beginPreview();
        } catch (error) {
          this.status = error.code === "session_expired" ? "expired" : "recovering";
          this.message = error.message;
          if (error.uncertain || error.code === "active_session_exists") await this.recover();
        } finally {
          this.busy = false;
          this.refreshDigits();
        }
      },
      beginPreview() {
        window.clearTimeout(this.previewTimer);
        this.phase = "preview";
        this.message = "Memorize the server-generated sequence. It will hide shortly.";
        const reduced = window.ArcadeProof.preferences.reducedMotion();
        this.previewTimer = window.setTimeout(() => {
          this.phase = "input";
          this.message = "Enter unique digits in order. The countdown is guidance; the server enforces expiry.";
        }, reduced ? 1200 : 2600);
      },
      enterDigit(event) {
        this.addDigit(Number(event.currentTarget.dataset.digit));
      },
      addDigit(digit) {
        if (!this.isInput || this.busy || this.lockedSubmission) return;
        if (!Number.isInteger(digit) || this.actions.includes(digit) || this.actions.length >= this.sequence.length) return;
        this.actions.push(digit);
        this.refreshDigits();
      },
      handleKey(event) {
        if (!this.isInput || document.querySelector("dialog[open]")) return;
        if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
        if (/^[0-9]$/.test(event.key)) {
          event.preventDefault();
          this.addDigit(Number(event.key));
        } else if (event.key === "Backspace") {
          event.preventDefault();
          this.undo();
        }
      },
      undo() {
        if (!this.isInput || this.busy || this.lockedSubmission) return;
        this.actions.pop();
        this.refreshDigits();
      },
      async submit() {
        if (!this.player || !this.session || !this.canSubmit) return;
        this.busy = true;
        this.status = "submitting";
        this.saveJournal({ actions: [...this.actions], pendingAction: "play" });
        try {
          this.outcome = await window.ArcadeProof.api.playSession(
            this.player.id,
            this.session.id,
            { actions: [...this.actions] },
          );
          this.phase = "result";
          this.status = "result";
          this.saveJournal({ outcome: this.outcome, pendingAction: "submit_score" });
          this.message = "Score returned by the server. Local speed is shown only for context and does not change the score.";
          const state = await window.ArcadeProof.api.getGameState(this.player.id, gameKey);
          this.reward = state.session?.reward || null;
          await this.submitFinalScore();
          window.setTimeout(() => document.getElementById("skill-result-title")?.focus(), 0);
        } catch (error) {
          this.status = error.code === "session_expired" ? "expired" : "recovering";
          this.message = error.message;
          if (error.uncertain) await this.recover();
        } finally {
          this.busy = false;
          this.refreshDigits();
        }
      },
      async submitFinalScore() {
        await window.ArcadeProof.finalScores.submitOrRecover(this, gameKey);
      },
      async claim() {
        if (!this.player || !this.session || !this.canClaim) return;
        this.busy = true;
        try {
          this.reward = await window.ArcadeProof.api.claimSession(this.player.id, this.session.id);
          window.ArcadeProof.journal.discard(this.player.id, gameKey);
          window.ArcadeProof.notify(`${this.reward.value} points claimed.`, "success");
        } catch (error) {
          this.message = error.message;
        } finally {
          this.busy = false;
          this.refreshDigits();
        }
      },
    };
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.games = window.ArcadeProof.games || {};
  window.ArcadeProof.games.skillCheck = skillCheck;
})();
