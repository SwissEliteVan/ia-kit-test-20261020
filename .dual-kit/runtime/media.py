# scripts/media/build_media.py
"""Conversion contrôlée en staging : --execute obligatoire pour écrire."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

IMAGES = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.webp'}
VIDEOS = {'.mp4', '.mov', '.mkv', '.webm', '.m4v'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def slug(text):
    text = unicodedata.normalize('NFKD', text)
    text = text.encode('ascii', 'ignore').decode('ascii').lower()
    return re.sub(r'[^a-z0-9]+', '-', text).strip('-') or 'media'


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode:
        raise RuntimeError(result.stderr[-1500:])


def probe(path):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
         'format=duration:stream=width,height,codec_name', '-of', 'json', str(path)],
         capture_output=True, text=True, timeout=40, check=True)
    return json.loads(p.stdout)


def record(path, original, kind):
    return {'path': f'{path.parent.name}/{path.name}', 'bytes': path.stat().st_size,
            'sha256': digest(path), 'from': original, 'format': kind}


def image_variants(source, dest, rel, execute):
    if not execute:
        return [{'planned': str(dest / (slug(source.stem) + '-720.webp'))}]
    from PIL import Image, ImageOps
    products = []
    with Image.open(source) as source_image:
        image = ImageOps.exif_transpose(source_image)
        if image.mode not in ('RGB', 'RGBA'):
            image = image.convert('RGB')
        for width in sorted({min(w, image.width) for w in (480, 960, 1440)}):
            h = max(1, round(image.height * min(1, width / image.width)))
            w = min(width, image.width)
            resized = image.resize((w, h), Image.Resampling.LANCZOS)
            for fmt, ext, opts in [('WEBP', '.webp', {'quality': 80, 'method': 6}),
                                   ('AVIF', '.avif', {'quality': 48})]:
                output = dest / (slug(source.stem) + '-' + str(w) + ext)
                if output.exists():
                    raise ValueError('Sortie existante : contrôler puis choisir un staging neuf')
                try:
                    resized.save(output, format=fmt, **opts)
                    products.append(record(output, rel, fmt))
                except (OSError, ValueError) as err:
                    if fmt == 'WEBP':
                        raise
                    print('AVIF indisponible :', source.name, err)
    return products


def video_variants(source, dest, rel, role, execute):
    if not execute:
        return [{'planned': str(dest / (slug(source.stem) + '-720.mp4'))}], {}
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('ffmpeg et ffprobe requis pour les vidéos')
    info = probe(source)
    products = []
    stream = next((s for s in info.get('streams', []) if s.get('width')), {})
    actual_width = int(stream.get('width', 0))
    if actual_width < 16:
        raise ValueError('Largeur video invalide ou absente')
    for width in sorted({min(w, actual_width) for w in (720, 1280)}):
        for ext, args in [('.mp4', ['-c:v','libx264','-crf','25','-preset','medium',
                                     '-pix_fmt','yuv420p','-movflags','+faststart']),
                          ('.webm', ['-c:v','libvpx-vp9','-crf','33','-b:v','0'])]:
            output = dest / (slug(source.stem) + '-' + str(width) + ext)
            if output.exists():
                raise ValueError('Sortie existante : choisir un staging neuf')
            if not output.exists():
                run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-n',
                     '-i',str(source),'-map','0:v:0','-an',
                     '-vf',f'scale=w=min({width}\\,iw):h=-2:flags=lanczos',
                     *args,str(output)])
            products.append(record(output, rel, ext.lstrip('.')))
    if role == 'hero':
        poster = dest / (slug(source.stem) + '-poster.jpg')
        if poster.exists():
            raise ValueError('Poster existant : choisir un staging neuf')
        if not poster.exists():
            run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-n',
                 '-ss','0.5','-i',str(source),'-frames:v','1','-q:v','3',str(poster)])
        products.append(record(poster, rel, 'poster'))
    return products, info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='assets-source')
    parser.add_argument('--staging', default='media-staging')
    parser.add_argument('--require-hero', action='store_true')
    parser.add_argument('--execute', action='store_true', help='écrit les dérivés en staging')
    args = parser.parse_args()
    if Path(args.staging).is_symlink():
        raise SystemExit('Staging symbolique refusé')
    src = Path(args.source).resolve()
    staging = Path(args.staging).resolve()
    if not src.is_dir() or src == staging or src in staging.parents or staging in src.parents:
        raise SystemExit('Source/staging absents, identiques ou imbriqués')
    if staging.exists() or staging.is_symlink():
        raise SystemExit('Staging existant : preuves conservées, choisir un emplacement neuf')
    rights_path = src / 'media-rights.json'
    rights = json.loads(rights_path.read_text(encoding='utf-8')) if rights_path.exists() else {}
    outputs = []
    blocked = []
    has_hero = False
    if args.execute:
        staging.mkdir(parents=True, exist_ok=False)
    for source in sorted(src.rglob('*')):
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(src):
            continue
        ext = source.suffix.lower()
        if ext not in IMAGES | VIDEOS:
            continue
        rel = source.relative_to(src).as_posix()
        details = rights.get(rel, {})
        if details.get('status') != 'verified' or not details.get('source') or not details.get('license'):
            blocked.append({'path': rel, 'reason': 'droits non vérifiés'})
            continue
        role = details.get('role', 'section')
        has_hero = has_hero or (role == 'hero' and ext in VIDEOS)
        # Sous-répertoire stable par chemin : pas de collision de noms entre dossiers
        unique = hashlib.sha256((rel + digest(source) + 'recipe-v1').encode('utf-8')).hexdigest()[:20]
        dest = staging / unique
        if args.execute:
                dest.mkdir(parents=True, exist_ok=False)
        try:
            if ext in IMAGES:
                products = image_variants(source, dest, rel, args.execute)
                meta = {}
            else:
                products, meta = video_variants(source, dest, rel, role, args.execute)
            if not products:
                raise ValueError('Aucun format de sortie compatible genere')
            outputs.append({'original': rel, 'source_sha256': digest(source),
                            'role': role, 'output': products, 'video_info': meta})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as err:
            blocked.append({'path': rel, 'reason': 'conversion : ' + str(err)[:300]})
    manifest = {'execute': args.execute, 'files': outputs, 'blocked': blocked,
                'hero_source_verified': has_hero}
    if args.execute:
        with (staging / 'manifest.json').open('x', encoding='utf-8') as handle:
            handle.write(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(json.dumps({'eligible_or_processed': len(outputs), 'blocked': len(blocked),
                      'hero_source_verified': has_hero, 'execute': args.execute}, ensure_ascii=False))
    if blocked or not outputs or (args.require_hero and not has_hero):
        raise SystemExit('REVIEW_REQUIRED: aucun média, droits bloqués ou hero requis manquant')


if __name__ == '__main__':
    main()
