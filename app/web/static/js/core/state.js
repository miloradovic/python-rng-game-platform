"use strict";

(() => {
  const states = Object.freeze({
    LOADING: "loading",
    READY: "ready",
    SUBMITTING: "submitting",
    ANIMATING: "animating",
    RESULT: "result",
    RECOVERING: "recovering",
    COOLDOWN: "cooldown",
    EXPIRED: "expired",
    TERMINAL_ERROR: "terminal_error",
  });

  const transitions = Object.freeze({
    loading: ["ready", "recovering", "terminal_error"],
    ready: ["submitting", "recovering", "cooldown", "expired"],
    submitting: ["animating", "result", "recovering", "terminal_error"],
    animating: ["result", "terminal_error"],
    result: ["ready", "submitting", "cooldown"],
    recovering: ["ready", "result", "cooldown", "expired", "terminal_error"],
    cooldown: ["ready", "recovering"],
    expired: ["ready", "recovering"],
    terminal_error: ["ready", "recovering"],
  });

  class BrowserState {
    constructor(initial = states.LOADING) {
      this.value = initial;
    }

    transition(next) {
      if (!(transitions[this.value] || []).includes(next)) {
        throw new Error(`Invalid browser state transition: ${this.value} -> ${next}`);
      }
      this.value = next;
      return this.value;
    }
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.states = states;
  window.ArcadeProof.BrowserState = BrowserState;
})();
