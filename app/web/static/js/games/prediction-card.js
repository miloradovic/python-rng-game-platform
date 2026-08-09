"use strict";

(() => {
  const gameKey = "prediction_card";

  function predictionCard() {
    return {
      busy: false,
      status: "loading",
      message: "Loading the available predictions…",
      config: null,
      session: null,
      outcome: null,
      reward: null,
      finalScore: null,
      pendingChoice: null,
      choices: [],
      cardClass: "prediction-card",
      cardSymbol: "?",
      resultLabel: "",
      countdownLabel: "",
      timer: null,
      get player() { return window.ArcadeProof.playerStore.current(); },
      get canChoose() {
        return !this.busy && !this.pendingChoice && ["ready", "result"].includes(this.status);
      },
      get hasPendingChoice() { return Boolean(this.pendingChoice && this.session?.status === "active"); },
      get resumeLabel() { return this.pendingChoice ? `Retry saved ${this.pendingChoice} choice` : "Retry saved choice"; },
      get countdownPrefix() {
        return this.session?.status === "active" ? "Active prediction expires in" : "Next prediction in";
      },
      get canClaim() { return Boolean(this.reward && this.reward.status === "pending"); },
      get isResult() { return Boolean(this.outcome); },
      get rewardLabel() { return this.reward ? `${this.reward.value} points · ${this.reward.status}` : ""; },
      async initialize() {
        window.addEventListener("arcade-proof:player", () => this.load());
        await this.load();
      },
      async load() {
        this.busy = true;
        try {
          this.config = await window.ArcadeProof.api.getGameConfig(gameKey);
          this.choices = (this.config.payload.choices || []).map((choice) => ({
            value: choice,
            label: choice === "red" ? "Red diamond" : "Black spade",
            symbol: choice === "red" ? "♦" : "♠",
            className: `prediction-choice ${choice}`,
          }));
          await this.recover();
        } catch (error) {
          this.status = "terminal_error";
          this.message = error.message;
        } finally {
          this.busy = false;
        }
      },
      saveJournal(values) {
        if (!this.player) return;
        const current = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
        window.ArcadeProof.journal.save(this.player.id, gameKey, { ...current, ...values });
      },
      async recover() {
        window.clearInterval(this.timer);
        this.countdownLabel = "";
        if (!this.player) {
          this.status = "ready";
          this.message = "Choose a local demo player, then predict red or black.";
          return;
        }
        this.status = "recovering";
        const state = await window.ArcadeProof.api.getGameState(this.player.id, gameKey);
        this.session = state.session;
        this.outcome = state.session?.outcome || null;
        this.reward = state.session?.reward || null;
        this.finalScore = state.session?.final_score || null;
        const saved = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
        this.pendingChoice = (!this.session || this.session.status === "active")
          && ["create_session", "play"].includes(saved.pendingAction)
          ? saved.choice
          : null;
        const countdownTarget = this.session?.status === "active"
          ? this.session.expires_at
          : state.next_play_at;
        if (countdownTarget) this.startCountdown(state.server_time, countdownTarget);
        if (!this.session) {
          this.status = state.next_play_at ? "cooldown" : "ready";
          this.message = "Choose a card color. The server result is fixed only after submission.";
          return;
        }
        this.saveJournal({
          requestId: this.session.request_id,
          sessionId: this.session.id,
          outcome: this.outcome,
          pendingAction: this.session.status === "active"
            ? saved.pendingAction
            : (this.reward?.status === "pending" ? "claim" : null),
        });
        if (this.session.status === "active") {
          this.status = "ready";
          this.message = this.pendingChoice
            ? `Your saved ${this.pendingChoice} request is ready for an exact retry.`
            : "Your active prediction was recovered. Choose once to finish it.";
        } else if (this.session.status === "expired") {
          this.status = "expired";
          this.message = "That prediction expired before a choice reached the server.";
        } else if (this.outcome) {
          this.presentResult(false);
          this.status = state.next_play_at ? "cooldown" : "result";
          this.message = "The authoritative card result was recovered.";
          if (!this.finalScore) {
            await window.ArcadeProof.finalScores.submitOrRecover(this, gameKey, state);
          }
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
      async choose(event) {
        if (this.busy) return;
        if (!this.player) {
          document.getElementById("identity-dialog")?.showModal();
          return;
        }
        const choice = event.currentTarget.dataset.choice;
        if (!this.choices.some((candidate) => candidate.value === choice)) return;
        await this.playChoice(choice);
      },
      async resume() {
        const choice = this.pendingChoice;
        if (!choice || this.busy) return;
        this.busy = true;
        try {
          await this.recover();
        } catch (error) {
          this.status = "recovering";
          this.message = error.message;
          return;
        } finally {
          this.busy = false;
        }
        if (!this.outcome && (!this.session || this.session.status === "active")) {
          await this.playChoice(choice);
        }
      },
      async playChoice(choice) {
        if (this.busy) return;
        this.busy = true;
        this.status = "submitting";
        try {
          if (!this.session || this.session.status !== "active") {
            this.outcome = null;
            this.reward = null;
            this.finalScore = null;
            this.cardClass = "prediction-card";
            this.cardSymbol = "?";
            this.resultLabel = "";
            this.pendingChoice = choice;
            const requestId = crypto.randomUUID();
            this.saveJournal({ requestId, choice, pendingAction: "create_session" });
            this.session = await window.ArcadeProof.api.createSession(this.player.id, requestId, gameKey);
            this.saveJournal({ sessionId: this.session.id, choice, pendingAction: "play" });
          } else {
            this.pendingChoice = choice;
            this.saveJournal({ choice, pendingAction: "play" });
          }
          this.outcome = await window.ArcadeProof.api.playSession(
            this.player.id,
            this.session.id,
            { choice },
          );
          this.saveJournal({ outcome: this.outcome, pendingAction: "submit_score" });
          this.pendingChoice = null;
          this.status = "animating";
          const duration = this.presentResult(true);
          if (duration) await new Promise((resolve) => window.setTimeout(resolve, duration));
          this.status = "result";
          this.message = this.outcome.result.correct
            ? "Correct prediction. The server awarded the configured reward."
            : "Not correct this time. The recorded reward is zero points.";
          window.setTimeout(
            () => document.getElementById("prediction-result-title")?.focus(),
            0,
          );
          await this.recoverReward();
          await window.ArcadeProof.finalScores.submitOrRecover(this, gameKey);
        } catch (error) {
          this.status = error.code === "session_expired" ? "expired" : "recovering";
          this.message = error.message;
          if (
            error.uncertain
            || error.code === "active_session_exists"
            || error.code === "invalid_transition"
          ) await this.recover();
        } finally {
          this.busy = false;
        }
      },
      presentResult(animate = true) {
        const result = this.outcome?.result;
        if (!result) return 0;
        const authoritative = result.authoritative_choice;
        this.cardSymbol = authoritative === "red" ? "♦" : "♠";
        this.cardClass = `prediction-card is-revealed ${authoritative}`;
        this.resultLabel = `${result.correct ? "Correct" : "Incorrect"}: you chose ${result.player_choice}; the server card was ${authoritative}.`;
        const reduced = window.ArcadeProof.preferences.reducedMotion();
        return animate && !reduced ? 700 : 0;
      },
      async recoverReward() {
        if (!this.player) return;
        const state = await window.ArcadeProof.api.getGameState(this.player.id, gameKey);
        this.reward = state.session?.reward || null;
      },
      async claim() {
        if (!this.player || !this.session || !this.canClaim) return;
        this.busy = true;
        try {
          this.reward = await window.ArcadeProof.api.claimSession(this.player.id, this.session.id);
          window.ArcadeProof.journal.discard(this.player.id, gameKey);
          window.ArcadeProof.notify(`${this.reward.value} points claimed.`, this.reward.value ? "success" : "info");
        } catch (error) {
          this.message = error.message;
        } finally {
          this.busy = false;
        }
      },
    };
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.games = window.ArcadeProof.games || {};
  window.ArcadeProof.games.predictionCard = predictionCard;
})();
