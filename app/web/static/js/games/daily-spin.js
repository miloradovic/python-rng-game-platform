"use strict";

(() => {
  const gameKey = "daily_spin";
  const palette = ["#67f1cf", "#ffbf5a", "#a987ff", "#ff7f73", "#63b3ff", "#f887d1"];
  const svgNamespace = "http://www.w3.org/2000/svg";

  function clientSeed() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function point(angle, radius = 108) {
    const radians = (angle * Math.PI) / 180;
    return [120 + radius * Math.cos(radians), 120 + radius * Math.sin(radians)];
  }

  function arcPath(start, end) {
    const [startX, startY] = point(start);
    const [endX, endY] = point(end);
    const largeArc = end - start > 180 ? 1 : 0;
    return `M 120 120 L ${startX} ${startY} A 108 108 0 ${largeArc} 1 ${endX} ${endY} Z`;
  }

  function renderWheelSegments(segments) {
    const container = document.getElementById("daily-spin-segments");
    if (!container) return;
    const groups = segments.map((segment) => {
      const group = document.createElementNS(svgNamespace, "g");
      const path = document.createElementNS(svgNamespace, "path");
      const label = document.createElementNS(svgNamespace, "text");
      group.setAttribute("data-segment-key", segment.key);
      path.setAttribute("d", segment.path);
      path.setAttribute("fill", segment.fill);
      label.setAttribute("x", segment.labelX);
      label.setAttribute("y", segment.labelY);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("dominant-baseline", "middle");
      label.textContent = String(segment.value);
      group.append(path, label);
      return group;
    });
    container.replaceChildren(...groups);
  }

  function dailySpin() {
    return {
      busy: false,
      status: "loading",
      message: "Getting the wheel ready…",
      config: null,
      session: null,
      pendingOutcome: null,
      pendingReward: null,
      outcome: null,
      reward: null,
      finalScore: null,
      proof: null,
      verification: null,
      segments: [],
      wheelRotation: 0,
      wheelAnimation: null,
      settledAnimation: null,
      presentationGeneration: 0,
      playerListener: null,
      resultVisible: false,
      resultLabel: "",
      countdownLabel: "",
      timer: null,
      get player() { return window.ArcadeProof.playerStore.current(); },
      get canPlay() { return !this.busy && ["ready", "result"].includes(this.status); },
      get canClaim() { return Boolean(this.reward && this.reward.status === "pending"); },
      get isVerified() { return Boolean(this.verification?.verified); },
      get hasProof() { return Boolean(this.proof); },
      get countdownPrefix() {
        return this.session?.status === "active" ? "Active spin expires in" : "Next spin in";
      },
      get actionLabel() {
        if (this.session?.status === "active") return "Resume spin";
        return "Spin now";
      },
      get rewardLabel() {
        if (!this.reward) return "Points are being prepared";
        return this.reward.status === "pending"
          ? `${this.reward.value} points ready to collect`
          : `${this.reward.value} points collected`;
      },
      get proofText() { return this.proof ? JSON.stringify(this.proof, null, 2) : ""; },
      async initialize() {
        this.playerListener = () => {
          this.cancelPresentation();
          void this.load();
        };
        window.addEventListener("arcade-proof:player", this.playerListener);
        await this.load();
      },
      destroy() {
        this.cancelPresentation();
        if (this.playerListener) {
          window.removeEventListener("arcade-proof:player", this.playerListener);
        }
        window.clearInterval(this.timer);
      },
      cancelPresentation() {
        this.presentationGeneration += 1;
        this.wheelAnimation?.cancel();
        this.settledAnimation?.cancel();
        this.wheelAnimation = null;
        this.settledAnimation = null;
      },
      resetWheel() {
        this.cancelPresentation();
        this.wheelRotation = 0;
      },
      async load() {
        this.cancelPresentation();
        const loadGeneration = this.presentationGeneration;
        const loadPlayerId = this.player?.id || null;
        this.busy = true;
        this.resultVisible = false;
        this.resultLabel = "";
        this.outcome = null;
        this.reward = null;
        this.pendingOutcome = null;
        this.pendingReward = null;
        this.proof = null;
        this.verification = null;
        this.message = "Getting the wheel ready…";
        try {
          this.config = await window.ArcadeProof.api.getGameConfig(gameKey);
          if (loadGeneration !== this.presentationGeneration) return;
          this.buildSegments();
          await this.recover(loadGeneration, loadPlayerId);
        } catch (error) {
          if (loadGeneration !== this.presentationGeneration) return;
          this.status = "terminal_error";
          this.message = error.message;
        } finally {
          if (loadGeneration === this.presentationGeneration) this.busy = false;
        }
      },
      buildSegments() {
        const rewards = this.config?.payload?.rewards || [];
        const total = rewards.reduce((sum, band) => sum + band.weight, 0);
        let cursor = -90;
        this.segments = rewards.map((band, index) => {
          const sweep = (band.weight / total) * 360;
          const center = cursor + sweep / 2;
          const [labelX, labelY] = point(center, 68);
          const segment = {
            ...band,
            path: arcPath(cursor, cursor + sweep),
            fill: palette[index % palette.length],
            labelX,
            labelY,
            percentage: `${Math.round((band.weight / total) * 100)}%`,
            center,
          };
          cursor += sweep;
          return segment;
        });
        renderWheelSegments(this.segments);
      },
      saveJournal(values) {
        if (!this.player) return;
        const current = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
        window.ArcadeProof.journal.save(this.player.id, gameKey, { ...current, ...values });
      },
      async recover(
        generation = this.presentationGeneration,
        expectedPlayerId = this.player?.id || null,
      ) {
        window.clearInterval(this.timer);
        this.countdownLabel = "";
        if (!this.player) {
          this.status = "ready";
          this.message = "Choose a player to spin the wheel.";
          return;
        }
        this.status = "recovering";
        const state = await window.ArcadeProof.api.getGameState(this.player.id, gameKey);
        if (
          generation !== this.presentationGeneration
          || (this.player?.id || null) !== expectedPlayerId
        ) return;
        this.session = state.session;
        this.pendingOutcome = state.session?.outcome || null;
        this.pendingReward = state.session?.reward || null;
        this.finalScore = state.session?.final_score || null;
        const countdownTarget = this.session?.status === "active"
          ? this.session.expires_at
          : state.next_play_at;
        if (countdownTarget) this.startCountdown(state.server_time, countdownTarget);
        if (!this.session) {
          this.status = state.next_play_at ? "cooldown" : "ready";
          this.message = "The wheel is ready. Take your spin.";
          return;
        }
        this.saveJournal({
          requestId: this.session.request_id,
          sessionId: this.session.id,
          commitmentId: this.session.fairness?.proof_id || null,
          outcome: this.pendingOutcome,
        });
        if (this.session.status === "active") {
          this.status = "ready";
          this.message = this.session.fairness
            ? "Your spin is saved and ready to resume."
            : "Your unfinished spin is ready to resume.";
          return;
        }
        if (this.session.status === "expired") {
          this.status = "expired";
          this.message = "That spin expired before it finished. Try again when the timer ends.";
          return;
        }
        if (this.pendingOutcome) {
          const revealed = await this.revealRecoveredResult(generation);
          if (!revealed) return;
          if (!this.finalScore) {
            await window.ArcadeProof.finalScores.submitOrRecover(this, gameKey, state);
          }
          await this.loadProof();
          this.status = state.next_play_at ? "cooldown" : "result";
          this.message = "Your finished spin is back on screen.";
          return;
        }
        this.status = state.next_play_at ? "cooldown" : "ready";
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
      async action() {
        if (this.busy) return;
        if (!this.player) {
          document.getElementById("identity-dialog")?.showModal();
          return;
        }
        this.busy = true;
        this.status = "submitting";
        this.message = "Starting your spin…";
        const actionPlayerId = this.player.id;
        try {
          let journal = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
          if (!this.session || this.session.status !== "active") {
            this.outcome = null;
            this.reward = null;
            this.pendingOutcome = null;
            this.pendingReward = null;
            this.finalScore = null;
            this.proof = null;
            this.verification = null;
            this.resultLabel = "";
            this.resultVisible = false;
            this.resetWheel();
            const requestId = crypto.randomUUID();
            this.saveJournal({ requestId, pendingAction: "create_session" });
            this.session = await window.ArcadeProof.api.createSession(actionPlayerId, requestId, gameKey);
            if (this.player?.id !== actionPlayerId) return;
            this.saveJournal({ sessionId: this.session.id, pendingAction: "commit" });
            journal = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
          }
          let proofId = this.session.fairness?.proof_id || journal.commitmentId;
          if (!proofId) {
            const commitment = await window.ArcadeProof.api.commitFairness(
              actionPlayerId,
              this.session.id,
            );
            if (this.player?.id !== actionPlayerId) return;
            proofId = commitment.proof_id;
            this.saveJournal({ commitmentId: proofId, pendingAction: "evaluate" });
          }
          journal = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
          const seed = journal.clientSeed || clientSeed();
          this.saveJournal({ clientSeed: seed, pendingAction: "evaluate" });
          this.message = "The wheel is finding your prize…";
          const evaluated = await window.ArcadeProof.api.evaluateFairness(
            actionPlayerId,
            proofId,
            seed,
          );
          if (this.player?.id !== actionPlayerId) return;
          this.pendingOutcome = evaluated.outcome;
          this.pendingReward = evaluated.reward;
          this.saveJournal({ outcome: this.pendingOutcome, pendingAction: "submit_score" });
          const revealed = await this.animateResult(this.presentationGeneration);
          if (!revealed || this.player?.id !== actionPlayerId) return;
          await window.ArcadeProof.finalScores.submitOrRecover(this, gameKey);
          await this.loadProof();
          this.status = "result";
          this.message = this.isVerified
            ? "Your result is ready and its fairness check passed."
            : "Your result is ready. Fairness details need another check.";
        } catch (error) {
          this.cancelPresentation();
          this.status = error.code === "session_expired" ? "expired" : "recovering";
          this.message = error.message;
          if (error.uncertain || error.code === "active_session_exists") await this.recover();
        } finally {
          if (this.player?.id === actionPlayerId) this.busy = false;
        }
      },
      segmentForPendingOutcome() {
        const rewardKey = this.pendingOutcome?.result?.reward_key;
        return this.segments.find((candidate) => candidate.key === rewardKey) || null;
      },
      targetRotation(segment, turns = 0) {
        return turns * 360 + (-90 - segment.center);
      },
      revealPendingResult() {
        const segment = this.segmentForPendingOutcome();
        this.outcome = this.pendingOutcome;
        this.reward = this.pendingReward;
        this.resultLabel = segment
          ? `${segment.value} points from ${segment.key.replaceAll("_", " ")}`
          : "Your result is ready";
        this.resultVisible = true;
      },
      async focusResult() {
        await this.$nextTick();
        document.getElementById("spin-result-title")?.focus();
      },
      async landRecoveredWheel() {
        const segment = this.segmentForPendingOutcome();
        if (!segment) throw new Error("This spin result does not match the loaded wheel.");
        const wheel = document.getElementById("daily-spin-wheel");
        if (!wheel) return;
        const rotation = this.targetRotation(segment);
        this.wheelAnimation = wheel.animate(
          [{ transform: `rotate(${rotation}deg)` }],
          { duration: 0, fill: "forwards" },
        );
        this.wheelRotation = rotation;
        await this.wheelAnimation.finished;
      },
      async revealRecoveredResult(generation) {
        try {
          await this.landRecoveredWheel();
        } catch (error) {
          if (error.name === "AbortError") return false;
          throw error;
        }
        if (generation !== this.presentationGeneration) return false;
        this.revealPendingResult();
        await this.focusResult();
        return true;
      },
      async animateResult(generation) {
        this.status = "animating";
        this.resultVisible = false;
        this.resultLabel = "";
        const segment = this.segmentForPendingOutcome();
        const wheel = document.getElementById("daily-spin-wheel");
        if (!segment) throw new Error("This spin result does not match the loaded wheel.");
        if (!wheel) {
          this.revealPendingResult();
          await this.focusResult();
          return true;
        }
        if (window.ArcadeProof.preferences.reducedMotion()) {
          await this.landRecoveredWheel();
          if (generation !== this.presentationGeneration) return false;
          this.revealPendingResult();
          await this.focusResult();
          return true;
        }
        const start = this.wheelRotation;
        const finish = this.targetRotation(segment, 6);
        this.wheelAnimation = wheel.animate(
          [
            { offset: 0, transform: `rotate(${start}deg)`, easing: "cubic-bezier(0.42, 0, 1, 1)" },
            { offset: 0.14, transform: `rotate(${start + 220}deg)`, easing: "linear" },
            { offset: 0.56, transform: `rotate(${start + 1120}deg)`, easing: "cubic-bezier(0, 0, 0.2, 1)" },
            { offset: 0.92, transform: `rotate(${finish - 12}deg)`, easing: "cubic-bezier(0.2, 0, 0, 1)" },
            { offset: 1, transform: `rotate(${finish}deg)` },
          ],
          { duration: 2600, fill: "forwards" },
        );
        this.wheelRotation = finish;
        try {
          await this.wheelAnimation.finished;
          if (generation !== this.presentationGeneration) return false;
          this.settledAnimation = wheel.animate(
            [
              { filter: "brightness(1)" },
              { filter: "brightness(1.3) drop-shadow(0 0 14px rgba(103, 241, 207, 0.55))" },
              { filter: "brightness(1)" },
            ],
            { duration: 320, easing: "ease-out" },
          );
          await this.settledAnimation.finished;
        } catch (error) {
          if (error.name === "AbortError") return false;
          throw error;
        }
        if (generation !== this.presentationGeneration) return false;
        this.revealPendingResult();
        await this.focusResult();
        return true;
      },
      async loadProof() {
        if (!this.player || !this.outcome) return;
        this.proof = await window.ArcadeProof.api.getFairnessProof(this.player.id, this.outcome.id);
        this.verification = await window.ArcadeProof.api.verifyFairness(this.player.id, this.outcome.id);
        this.saveJournal({ pendingAction: this.canClaim ? "claim" : null });
      },
      async claim() {
        if (!this.player || !this.session || !this.canClaim) return;
        this.busy = true;
        this.saveJournal({ pendingAction: "claim" });
        try {
          this.reward = await window.ArcadeProof.api.claimSession(this.player.id, this.session.id);
          window.ArcadeProof.journal.discard(this.player.id, gameKey);
          window.ArcadeProof.notify(`${this.reward.value} points collected.`, "success");
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
  window.ArcadeProof.games.dailySpin = dailySpin;
})();
