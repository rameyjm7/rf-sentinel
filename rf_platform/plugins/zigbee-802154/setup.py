from __future__ import annotations

from setuptools import Extension, setup


setup(
    ext_modules=[
        Extension(
            "zigbee_802154._cdecode",
            ["src/zigbee_802154/_cdecode.c"],
            extra_compile_args=["-O3"],
        )
    ]
)
