#!/usr/bin/env python3
"""Regras de fila do @progressodiario_br, em codigo (para a rotina nao depender de interpretacao).

Uso (a partir da raiz do repositorio):
  python3 ferramentas/publicacao.py proximo reels|post [--ig arquivo.json]
  python3 ferramentas/publicacao.py marcar <pasta>
  python3 ferramentas/publicacao.py registrar <pasta> --media-id ID | --timeout
  python3 ferramentas/publicacao.py liberar <pasta>        (so para ERRO EXPLICITO que nao seja timeout)
  python3 ferramentas/publicacao.py pendencias [--ig arquivo.json]

Garantias:
  * uma pasta so e escolhida se o comeco da legenda (80 caracteres) NAO existir em postados/,
    no feed do Instagram (--ig) nem em outra pasta anterior da fila;
  * pastas repetidas sao movidas para postados/ com status: duplicado (sem midia), nunca publicadas;
  * `registrar` move a pasta para postados/ na hora: depois disso ela nao volta para a fila;
  * pasta com status: publicando NUNCA e escolhida; so volta a `pronto` depois de 24 h e se a legenda
    nao aparecer em lugar nenhum.
O script mexe nos arquivos e usa `git mv`/`git rm`/`git add`; quem chama faz commit e push.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path.cwd()
FUSO = ZoneInfo("America/Sao_Paulo")
MIDIAS = ("video.mp4", "01.jpg")


def agora() -> datetime:
    return datetime.now(FUSO)


def impressao(texto: str) -> str:
    base = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", base).strip()[:80]


def ler(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def legenda(texto: str) -> str:
    m = re.search(r"## Legenda\n\n(.*?)(?:\n\n## |\Z)", texto, re.S)
    return m.group(1).strip() if m else ""


def campo(texto: str, nome: str) -> str:
    m = re.match(r"---\n(.*?)\n---", texto, re.S)
    if not m:
        return ""
    for linha in m.group(1).split("\n"):
        if linha.startswith(nome + ":"):
            return linha.split(":", 1)[1].strip()
    return ""


def pastas(sub: str) -> list[Path]:
    base = RAIZ / sub
    return sorted(d for d in base.iterdir() if (d / "post.md").exists()) if base.exists() else []


def usados_repo() -> set[str]:
    return {impressao(legenda(ler(d / "post.md"))) for d in pastas("postados")} - {""}


def usados_ig(arquivo: str | None) -> set[str]:
    if not arquivo:
        return set()
    dados = json.loads(Path(arquivo).read_text(encoding="utf-8"))
    linhas = dados.get("data", dados) if isinstance(dados, dict) else dados
    return {impressao(str(i.get("media_caption") or "")) for i in linhas} - {""}


def git(*args: str) -> None:
    subprocess.run(["git", *args], cwd=RAIZ, check=True, capture_output=True)


def trocar_frontmatter(texto: str, **mudancas: str | None) -> str:
    m = re.match(r"---\n(.*?)\n---\n", texto, re.S)
    linhas = [l for l in m.group(1).split("\n") if l.split(":", 1)[0] not in mudancas]
    for k, v in mudancas.items():
        if v is not None:
            linhas.append(f"{k}: {v}")
    ordem = ["status", "tipo", "agendar_para", "postado_em", "link", "tentativa_em"]
    linhas.sort(key=lambda l: ordem.index(l.split(":", 1)[0]) if l.split(":", 1)[0] in ordem else 99)
    return "---\n" + "\n".join(linhas) + "\n---\n" + texto[m.end():]


def mover_para_postados(pasta: Path, **mudancas: str | None) -> Path:
    destino = RAIZ / "postados" / pasta.name
    post = pasta / "post.md"
    post.write_text(trocar_frontmatter(ler(post), **mudancas), encoding="utf-8", newline="\n")
    git("mv", str(pasta.relative_to(RAIZ)), str(destino.relative_to(RAIZ)))
    for m in MIDIAS:
        if (destino / m).exists():
            git("rm", "-qf", str((destino / m).relative_to(RAIZ)))
    git("add", str((destino / "post.md").relative_to(RAIZ)))
    return destino


def cmd_proximo(tipo: str, ig: str | None) -> int:
    tipo_post = {"reels": "reels", "reel": "reels", "post": "post", "foto": "post"}[tipo]
    usados = usados_repo() | usados_ig(ig)
    vistos: dict[str, str] = {}
    candidatos = []
    for d in sorted(pastas("fila"), key=lambda x: (campo(ler(x / "post.md"), "agendar_para"), x.name)):
        texto = ler(d / "post.md")
        status, imp = campo(texto, "status"), impressao(legenda(texto))
        if status not in ("pronto",):
            continue
        if imp in usados or imp in vistos:
            motivo = "ja publicado" if imp in usados else f"igual a {vistos[imp]}"
            mover_para_postados(d, status="duplicado", tentativa_em=None)
            print(f"DUPLICADO ignorado: {d.name} ({motivo})", file=sys.stderr)
            continue
        vistos[imp] = d.name
        if campo(texto, "tipo") == tipo_post and any((d / m).exists() for m in MIDIAS):
            candidatos.append(d)
    print(candidatos[0].name if candidatos else "NENHUM")
    return 0


def cmd_marcar(nome: str) -> int:
    d = RAIZ / "fila" / nome
    texto = ler(d / "post.md")
    if campo(texto, "status") != "pronto":
        print(f"RECUSADO: {nome} esta com status {campo(texto, 'status')!r}, nao 'pronto'")
        return 2
    (d / "post.md").write_text(trocar_frontmatter(texto, status="publicando", tentativa_em=agora().strftime("%Y-%m-%d %H:%M")),
                               encoding="utf-8", newline="\n")
    git("add", str((d / "post.md").relative_to(RAIZ)))
    print("OK marcado")
    return 0


def cmd_registrar(nome: str, media_id: str | None, timeout: bool) -> int:
    d = RAIZ / "fila" / nome
    if not d.exists():
        print(f"JA REGISTRADO ou inexistente: {nome}")
        return 0
    link = f"# media_id {media_id}" if media_id else "# media_id desconhecido (timeout do Windsor)"
    mover_para_postados(d, status="postado", postado_em=agora().strftime("%Y-%m-%d %H:%M"), link=link, tentativa_em=None)
    print("OK registrado em postados/")
    return 0


def cmd_liberar(nome: str) -> int:
    d = RAIZ / "fila" / nome
    texto = ler(d / "post.md")
    if campo(texto, "status") != "publicando":
        print(f"NADA A LIBERAR: {nome} esta com status {campo(texto, 'status')!r}")
        return 0
    (d / "post.md").write_text(trocar_frontmatter(texto, status="pronto", tentativa_em=None), encoding="utf-8", newline="
")
    git("add", str((d / "post.md").relative_to(RAIZ)))
    print("OK liberado (voltou para pronto)")
    return 0


def cmd_pendencias(ig: str | None) -> int:
    usados = usados_repo() | usados_ig(ig)
    for d in pastas("fila"):
        texto = ler(d / "post.md")
        if campo(texto, "status") != "publicando":
            continue
        t0 = datetime.strptime(campo(texto, "tentativa_em") or "2000-01-01 00:00", "%Y-%m-%d %H:%M").replace(tzinfo=FUSO)
        if impressao(legenda(texto)) in usados:
            mover_para_postados(d, status="postado", postado_em=t0.strftime("%Y-%m-%d %H:%M"),
                                link="# media_id desconhecido (encontrado no feed)", tentativa_em=None)
            print(f"REGISTRADO {d.name} (ja estava no feed)")
        elif agora() - t0 > timedelta(hours=24):
            (d / "post.md").write_text(trocar_frontmatter(texto, status="pronto", tentativa_em=None), encoding="utf-8", newline="\n")
            git("add", str((d / "post.md").relative_to(RAIZ)))
            print(f"LIBERADO {d.name} (mais de 24 h sem aparecer)")
        else:
            print(f"AGUARDAR {d.name} (tentativa recente; NAO publicar)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="cmd", required=True)
    a = s.add_parser("proximo"); a.add_argument("tipo", choices=["reels", "reel", "post", "foto"]); a.add_argument("--ig")
    s.add_parser("marcar").add_argument("pasta")
    r = s.add_parser("registrar"); r.add_argument("pasta"); r.add_argument("--media-id"); r.add_argument("--timeout", action="store_true")
    q = s.add_parser("pendencias"); q.add_argument("--ig")
    s.add_parser("liberar").add_argument("pasta")
    args = p.parse_args()
    if args.cmd == "proximo":
        return cmd_proximo(args.tipo, args.ig)
    if args.cmd == "marcar":
        return cmd_marcar(args.pasta)
    if args.cmd == "registrar":
        if not args.media_id and not args.timeout:
            p.error("informe --media-id ID ou --timeout")
        return cmd_registrar(args.pasta, args.media_id, args.timeout)
    if args.cmd == "liberar":
        return cmd_liberar(args.pasta)
    return cmd_pendencias(args.ig)


if __name__ == "__main__":
    sys.exit(main())
