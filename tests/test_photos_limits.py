# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 F. Javier R. Cabero
"""Límites por run de la sync de iCloud Fotos."""

from services.photos_limits import MAX_BACKOFF, Backoff, PhotosLimits, RunBudget

GB = 2**30


def _budget(**kw):
    sleeps = []
    return RunBudget(PhotosLimits(**kw), sleep=sleeps.append), sleeps


def test_tope_de_descargas_deja_el_resto_para_el_siguiente():
    b, sleeps = _budget(max_downloads=3, pause_s=0.5)
    done = 0
    for _ in range(5):
        if b.allow("descargas", 100):
            b.spend("descargas", 100)
            done += 1
    assert done == 3 and b.deferred["descargas"] == 2 and sleeps == [0.5] * 3
    assert not b.exhausted                      # quedarse sin cupo no es un error
    assert "quedan para la próxima vuelta: 2 descargas" in b.summary()


def test_tope_de_bytes_pero_el_primer_fichero_siempre_pasa():
    b, _ = _budget(max_bytes=1 * GB, pause_s=0)
    assert b.allow("descargas", 3 * GB)         # un vídeo más grande que el tope no se queda atascado
    b.spend("descargas", 3 * GB)
    assert not b.allow("descargas", 1)
    b2, _ = _budget(max_bytes=1 * GB, pause_s=0)
    b2.spend("subidas", GB // 2)
    assert b2.allow("descargas", GB // 4) and not b2.allow("descargas", GB)


def test_cupos_separados_por_tipo():
    b, _ = _budget(max_uploads=1, max_deletes=1, pause_s=0)
    b.spend("subidas")
    assert not b.allow("subidas") and b.allow("borrados") and b.allow("descargas")


def test_frenazos_cortan_el_run_y_doblan_la_espera():
    back = Backoff()
    b, _ = _budget(max_throttles=3)
    assert not b.throttled() and not b.throttled()
    assert b.throttled("HTTP 503") and b.exhausted
    assert not b.allow("descargas", 1)
    assert "Apple está frenando" in b.summary()
    assert back.after_run(b) == 2
    for _ in range(5):
        back.after_run(b)
    assert back.factor == MAX_BACKOFF
    tranquilo, _ = _budget()
    assert back.after_run(tranquilo) == 1      # un run sin frenazos vuelve al intervalo normal
