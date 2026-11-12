"""Hooks for the sites recipe."""


def noted(ctx, step, present):
    return Probe(True, "noted")
