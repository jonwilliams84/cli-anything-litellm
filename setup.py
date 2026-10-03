#!/usr/bin/env python3
"""Setup for cli-anything-litellm."""

from setuptools import find_namespace_packages, setup

setup(
    name="cli-anything-litellm",
    version="0.5.0",
    description="CLI harness for administering a LiteLLM proxy — models, routing, keys, spend, drift, policy",
    license="MIT",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    package_data={"cli_anything.litellm": ["skills/*.md"]},
    python_requires=">=3.10",
    install_requires=["click>=8.1", "requests>=2.28", "pyyaml>=6"],
    extras_require={"dev": ["pytest>=7", "ruff>=0.6"]},
    entry_points={"console_scripts": ["cli-anything-litellm=cli_anything.litellm.litellm_cli:main"]},
)
