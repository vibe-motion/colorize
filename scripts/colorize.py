#!/usr/bin/env python3
"""Colorize: black-and-white to color reveal with Tyndall spin focus.

Renders photos, videos and Live Photos (iPhone HEIC/JPG + MOV pairs, Android
motion photos) to H.264 MP4. Each clip starts monochrome and spun; color sweeps
in from the left under a horizontal motion blur, the spin settles from the
center outward, and a soft glow remains on the final full-color frame.

Requires Python 3.9+, numpy, moderngl, Pillow, ffmpeg and ffprobe.
"""
from pathlib import Path
import argparse, json, shutil, subprocess, sys, tempfile
import numpy as np
import moderngl
from PIL import Image, ImageOps

SKILL_DIR = Path(__file__).resolve().parents[1]
MAPS = SKILL_DIR/'assets/effect_maps'
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.heic', '.heif', '.avif'}
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi', '.gif'}

VERT = '''#version 330
in vec2 pos; out vec2 uv;
void main(){uv=pos*.5+.5;gl_Position=vec4(pos,0,1);}
'''
COMMON = '''#version 330
in vec2 uv; out vec4 frag;
uniform sampler2D src;
vec3 lut(sampler2D tex, vec3 c){
 c=clamp(c,0.,1.);float b=c.b*63.;
 vec2 q0=vec2(mod(floor(b),8.),floor(floor(b)/8.));
 vec2 q1=vec2(mod(ceil(b),8.),floor(ceil(b)/8.));
 vec2 p0=q0*.125+vec2(.5/512.)+(.125-1./512.)*c.rg;
 vec2 p1=q1*.125+vec2(.5/512.)+(.125-1./512.)*c.rg;
 return mix(texture(tex,p0).rgb,texture(tex,p1).rgb,fract(b));
}
'''
# Monochrome-to-color sweep with a soft, depth-like front.
GRAD = COMMON + '''
uniform sampler2D monoLut, colorLut;
uniform float edge, front, depth0, depthY, depthR, grade;
void main(){
 vec3 c=texture(src,uv).rgb;
 vec3 color=mix(c,lut(colorLut,c),grade);
 vec3 mono=lut(monoLut,c);
 float d=.75*uv.x+depth0+depthY*(uv.y-.5)+depthR*length(uv-.5);
 float m=1.-smoothstep(front-edge,front+edge,d);
 frag=vec4(mix(mono,color,m),1);
}
'''
# Horizontal motion blur; two passes are stacked per frame.
MOTION = COMMON + '''
uniform float stepUV, count, alpha;
uniform sampler2D original;
void main(){
 vec3 s=texture(src,uv).rgb;float n=1.;
 for(int i=1;i<=11;i++){
  if(float(i)>count)break;
  for(int side=-1;side<=1;side+=2){
   vec2 p=uv+vec2(float(i*side)*stepUV,0);
   if(p.x>=0.&&p.x<=1.){s+=texture(src,p).rgb;n+=1.;}
  }
 }
 frag=vec4(mix(texture(original,uv).rgb,s/n,alpha),1);
}
'''
ROTATE = COMMON + '''
uniform sampler2D focusMask;
uniform float angle, aspect, focusScale, centerX, centerY;
void main(){
 vec3 s=vec3(0);float n=0.;vec2 center=vec2(centerX,centerY);
 vec2 p=(uv-center)*vec2(aspect,1);
 for(int i=0;i<33;i++){
  // One-sided arc from 0 to angle. A centered kernel would cancel the
  // angular displacement and lose the visible turning motion.
  float a=angle*float(i)/32.;float cs=cos(a),sn=sin(a);
  vec2 q=vec2(cs*p.x-sn*p.y,sn*p.x+cs*p.y)/vec2(aspect,1)+center;
  if(all(greaterThanEqual(q,vec2(0)))&&all(lessThanEqual(q,vec2(1)))){s+=texture(src,q).rgb;n+=1.;}
 }
 // The focus disc is a square focusScale*max(w,h) wide at the frame center,
 // used as a luma matte for the unrotated layer. Its scale grows from zero,
 // so the sharp area spreads outward from the center.
 vec2 d=(uv-.5)*vec2(aspect,1)/max(aspect,1.);
 float keep=focusScale>0.?dot(texture(focusMask,.5+d/focusScale).rgb,vec3(.299,.587,.114)):0.;
 frag=vec4(mix(s/max(n,1.),texture(src,uv).rgb,keep),1);
}
'''
GLOW = COMMON + '''
uniform sampler2D tyndallLut;
uniform float bloom, radius, threshold, gain, gammaVal, lift, grade, softness, saturation, rays;
uniform vec2 resolution;
void main(){
 vec3 c=texture(src,uv).rgb;
 vec3 b=vec3(0);float n=0.;
 for(int y=-3;y<=3;y++)for(int x=-3;x<=3;x++){
  float w=exp(-float(x*x+y*y)/5.);
  vec3 a=texture(src,uv+vec2(x,y)*radius/resolution).rgb;
  float l=max(max(a.r,a.g),a.b);
  b+=a*smoothstep(threshold,1.,l)*w;n+=w;
 }
 vec3 ray=vec3(0);float rn=0.;
 for(int i=0;i<24;i++){
  float f=float(i)/23.;float w=exp(-f*1.);
  vec3 a=texture(src,mix(uv,vec2(.9,.92),f*.26)).rgb;
  ray+=a*smoothstep(.75,1.,max(max(a.r,a.g),a.b))*w;rn+=w;
 }
 vec3 soft=vec3(0);float sn=0.;
 for(int y=-1;y<=1;y++)for(int x=-1;x<=1;x++){
  float w=(x==0?2.:1.)*(y==0?2.:1.);
  soft+=texture(src,uv+vec2(x,y)*softness/resolution).rgb*w;sn+=w;
 }
 c=soft/sn;
 c=1.-(1.-c)*(1.-b/n*bloom-ray/rn*rays);
 c=mix(c,lut(tyndallLut,c),grade);
 c=mix(vec3(dot(c,vec3(.299,.587,.114))),c,saturation);
 c=pow(max(c,vec3(0)),vec3(gammaVal))*gain+lift;
 frag=vec4(clamp(c,0.,1.),1);
}
'''

# Fitted against Jianying exports of 单彩渐变 + 动感模糊 + 丁达尔旋焦 on static
# photos. Temporal values are interpolated, so any duration and fps work.
DEFAULT = dict(depth0=.144148, depthY=.052029, depthR=-.163598, edge=.236978,
               front_speed=.631164, front_start=-.35, grade=.1, angle=.050265,
               focus_scale=.9, motion=.700743, motion_range=.982808, bloom=.536758,
               radius=5., threshold=.5, gain=.952346, gamma=.981704, lift=0.,
               tyndall_grade=.437028, center_x=.5, center_y=.5, softness=2.307218,
               saturation=.982107, rays=.285462, blur_power=.995649, bloom_start=.298435)


class Renderer:
    def __init__(self, width, height):
        self.w, self.h = width, height
        try:
            self.ctx = moderngl.create_standalone_context(require=330)
        except Exception:
            # Headless Linux has no default GL context; EGL usually works there.
            self.ctx = moderngl.create_standalone_context(require=330, backend='egl')
        self.vbo = self.ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], np.float32).tobytes())
        self.programs, self.vaos = {}, {}
        for k, s in [('grad', GRAD), ('motion', MOTION), ('rotate', ROTATE), ('glow', GLOW)]:
            p = self.ctx.program(vertex_shader=VERT, fragment_shader=s)
            self.programs[k] = p
            self.vaos[k] = self.ctx.simple_vertex_array(p, self.vbo, 'pos')
        self.buffers = [self.ctx.texture((width, height), 3, dtype='f2') for _ in range(5)]
        self.fbos = [self.ctx.framebuffer(color_attachments=[t]) for t in self.buffers]
        self.maps = {}
        for key, file in [('monoLut', 'mono_lut.png'), ('colorLut', 'color_lut.png'),
                          ('tyndallLut', 'tyndall_lut.png'), ('focusMask', 'focus_mask.png')]:
            # LUT and mask rows stay in file order: they are texture
            # coordinates, not display coordinates.
            a = np.asarray(Image.open(MAPS/file).convert('RGB'))
            self.maps[key] = self.ctx.texture((a.shape[1], a.shape[0]), 3, a.tobytes())
        self.input = self.ctx.texture((width, height), 3)
        for tex in [*self.maps.values(), *self.buffers, self.input]:
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = False

    def set_frame(self, rgb):
        self.input.write(np.ascontiguousarray(rgb[::-1]).tobytes())

    def run(self, name, out, textures, values):
        p = self.programs[name]
        for i, (key, tex) in enumerate(textures.items()):
            if key in p:
                tex.use(i)
                p[key].value = i
        for key, value in values.items():
            if key in p:
                p[key].value = value
        self.fbos[out].use()
        self.ctx.viewport = (0, 0, self.w, self.h)
        self.vaos[name].render(moderngl.TRIANGLE_STRIP)
        return self.buffers[out]

    def render(self, q, p):
        """Render the current frame at reveal progress q in [0, 1]."""
        decay = 1-q
        scale = min(self.w, self.h)/1080
        g = self.run('grad', 0, {'src': self.input, **self.maps},
                     {**p, 'front': p['front_start']+2*q*p['front_speed']})
        # Two horizontal sampling passes: 70/100 displacement, 85 -> 0 strength.
        m = self.run('motion', 1, {'src': g, 'original': g},
                     dict(stepUV=2/720*(40/7)*p['motion_range'], count=8., alpha=1.))
        m = self.run('motion', 2, {'src': m, 'original': g},
                     dict(stepUV=2/720*p['motion_range'], count=7., alpha=p['motion']*decay))
        # Spin blur 40 -> 0 shortens the arc while atmosphere 0 -> 30 grows
        # the focus disc, so the frame settles from the center outward.
        r = self.run('rotate', 3, {'src': m, 'focusMask': self.maps['focusMask']},
                     dict(angle=p['angle']*decay**p['blur_power'], aspect=self.w/self.h,
                          focusScale=p['focus_scale']*q, centerX=p['center_x'], centerY=p['center_y']))
        glow = p['bloom_start']+(1-p['bloom_start'])*q
        self.run('glow', 4, {'src': r, **self.maps},
                 dict(bloom=p['bloom']*glow, radius=p['radius']*scale, threshold=p['threshold'],
                      gain=p['gain'], gammaVal=p['gamma'], lift=p['lift'], grade=p['tyndall_grade'],
                      resolution=(self.w, self.h), softness=p['softness']*scale,
                      saturation=p['saturation'], rays=p['rays']*glow))
        a = np.frombuffer(self.fbos[4].read(components=3, alignment=1), np.uint8)
        return a.reshape(self.h, self.w, 3)[::-1]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def has_filter(name):
    out = run(['ffmpeg', '-hide_banner', '-filters'], text=True).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines())


def probe(path):
    out = run(['ffprobe', '-v', 'error', '-show_entries',
               'stream=codec_type,width,height,color_transfer,nb_frames:stream_tags=rotate:'
               'stream_side_data=rotation:format=duration', '-of', 'json', str(path)], text=True)
    info = json.loads(out.stdout or '{}')
    streams = info.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if not v:
        sys.exit(f'No video stream in {path}')
    w, h = v['width'], v['height']
    rot = v.get('tags', {}).get('rotate') or next(
        (d['rotation'] for d in v.get('side_data_list', []) if 'rotation' in d), 0)
    if abs(round(float(rot))) % 180 == 90:
        w, h = h, w
    return dict(width=w, height=h, hdr=v.get('color_transfer') in ('arib-std-b67', 'smpte2084'),
                audio=any(s.get('codec_type') == 'audio' for s in streams),
                duration=float(info.get('format', {}).get('duration') or 0))


def load_image(path):
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).convert('RGB')
    except Exception:
        pass
    # HEIC/AVIF without pillow-heif: macOS sips, then ffmpeg.
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp)/'still.png'
        if shutil.which('sips'):
            run(['sips', '-s', 'format', 'png', str(path), '--out', str(png)])
        if not png.exists():
            run(['ffmpeg', '-v', 'error', '-y', '-i', str(path), '-frames:v', '1', str(png)])
        if not png.exists():
            sys.exit(f'Cannot decode image {path}; install pillow-heif for HEIC/AVIF')
        with Image.open(png) as im:
            return ImageOps.exif_transpose(im).convert('RGB')


def extract_motion_photo(path, tmp):
    """Return the MP4 appended to a Google/Samsung motion photo JPEG, if any."""
    data = path.read_bytes()
    if data[:2] != b'\xff\xd8':
        return None
    i = data.find(b'ftyp', 1024)
    while i != -1:
        size = int.from_bytes(data[i-4:i], 'big')
        if 8 <= size <= 256 and data[i+4:i+8].strip().isalnum():
            out = Path(tmp)/f'{path.stem}_motion.mp4'
            out.write_bytes(data[i-4:])
            return out
        i = data.find(b'ftyp', i+4)
    return None


def resolve(path, still, tmp):
    path = path.expanduser().resolve()
    if not path.exists():
        sys.exit(f'Not found: {path}')
    ext = path.suffix.lower()
    if ext in IMAGE_EXT and not still:
        # iPhone Live Photo: IMG_0001.HEIC + IMG_0001.MOV in the same folder.
        for e in ('.mov', '.MOV', '.mp4', '.MP4'):
            pair = path.with_suffix(e)
            if pair.exists():
                return dict(kind='video', path=pair, label=f'{path.name} (Live Photo)', **probe(pair))
        if ext in ('.jpg', '.jpeg'):
            mp4 = extract_motion_photo(path, tmp)
            if mp4:
                return dict(kind='video', path=mp4, label=f'{path.name} (motion photo)', **probe(mp4))
    if ext in VIDEO_EXT:
        return dict(kind='video', path=path, label=path.name, **probe(path))
    image = load_image(path)
    return dict(kind='image', path=path, label=path.name, image=image,
                width=image.width, height=image.height, audio=False)


def fit(image, w, h):
    s = max(w/image.width, h/image.height)
    size = (max(w, round(image.width*s)), max(h, round(image.height*s)))
    image = image.resize(size, Image.Resampling.LANCZOS if s < 1 else Image.Resampling.BICUBIC)
    x, y = (size[0]-w)//2, (size[1]-h)//2
    return np.asarray(image.crop((x, y, x+w, y+h)))


def video_frames(src, w, h, fps, max_length):
    vf = []
    if src['hdr']:
        if has_filter('zscale') and has_filter('tonemap'):
            vf.append('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
                      'tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv')
        else:
            print(f'Warning: {src["label"]} is HDR and this ffmpeg lacks zscale; colors may look flat', flush=True)
    vf += [f'fps={fps}', f'scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos',
           f'crop={w}:{h}', 'setsar=1', 'format=rgb24']
    cmd = ['ffmpeg', '-v', 'error', '-i', str(src['path'])]
    cmd += ['-t', str(max_length)] if max_length else []
    cmd += ['-vf', ','.join(vf), '-an', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = w*h*3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        proc.wait()


def encoder(path, w, h, fps, crf, audio=None, audio_length=None):
    """audio: None, 'silent', or a media path whose first audio stream is kept."""
    cmd = ['ffmpeg', '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
           '-s', f'{w}x{h}', '-r', str(fps), '-i', '-']
    if audio == 'silent':
        cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
    elif audio:
        cmd += (['-t', str(audio_length)] if audio_length else []) + ['-i', str(audio)]
    cmd += ['-map', '0:v:0']
    if audio:
        # apad + shortest: the clip always ends with the last video frame.
        cmd += ['-map', '1:a:0', '-af', 'apad', '-c:a', 'aac', '-b:a', '192k',
                '-ar', '48000', '-ac', '2', '-shortest']
    cmd += ['-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p',
            '-c:v', 'libx264', '-preset', 'medium', '-crf', str(crf), '-movflags', '+faststart',
            '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', str(path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def render_source(renderer, src, params, args, out, audio):
    w, h, fps = renderer.w, renderer.h, args.fps
    reveal = max(2, round(args.duration*fps))
    if src['kind'] == 'image':
        frames = None
        total = reveal+round(args.hold*fps)
        renderer.set_frame(fit(src['image'], w, h))
    else:
        length = min(src['duration'], args.max_length or src['duration'])
        estimate = int(length*fps)
        if 2 <= estimate < reveal:
            print(f'Note: {src["label"]} is shorter than the reveal; compressing it to {length:.2f}s', flush=True)
            reveal = estimate
        frames = video_frames(src, w, h, fps, args.max_length)
        total = None
    track = src['path'] if audio and src['audio'] else 'silent' if audio else None
    enc = encoder(out, w, h, fps, args.crf, track, args.max_length)
    count, settled = 0, None
    try:
        while total is None or count < total:
            if frames is not None:
                frame = next(frames, None)
                if frame is None:
                    break
                renderer.set_frame(frame)
            q = min(count/(reveal-1), 1.)
            if frames is None and q == 1 and settled is not None:
                out_frame = settled  # still image: the settled frame never changes
            else:
                out_frame = renderer.render(q, params)
                if q == 1:
                    settled = out_frame
            enc.stdin.write(np.ascontiguousarray(out_frame).tobytes())
            count += 1
    finally:
        enc.stdin.close()
    if enc.wait():
        sys.exit(f'ffmpeg failed while encoding {src["label"]}')
    if count < 2:
        sys.exit(f'No frames decoded from {src["label"]}')
    print(f'Rendered {src["label"]}: {src["kind"]}, {count} frames', flush=True)


def even(x):
    return max(2, int(round(x/2))*2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inputs', nargs='+', type=Path, help='images, videos or Live Photo stills; several inputs are joined in order')
    ap.add_argument('-o', '--output', type=Path, help='MP4 path (default: <first input>_colorize.mp4 in the current folder)')
    ap.add_argument('--duration', type=float, default=2., help='reveal length in seconds (default 2)')
    ap.add_argument('--hold', type=float, default=0., help='images: seconds to hold the settled frame after the reveal')
    ap.add_argument('--fps', type=int, default=30)
    ap.add_argument('--size', help='force WxH, center-cropped (default: first input aspect, long edge <= --max-edge)')
    ap.add_argument('--max-edge', type=int, default=1920)
    ap.add_argument('--max-length', type=float, help='videos: keep at most this many seconds')
    ap.add_argument('--still', action='store_true', help='ignore Live Photo / motion photo video; use the still only')
    ap.add_argument('--mute', action='store_true', help='drop audio from video inputs')
    ap.add_argument('--params', type=Path, help='JSON file of effect parameters to override')
    ap.add_argument('--set', action='append', default=[], metavar='KEY=VALUE', help='override one effect parameter')
    ap.add_argument('--crf', type=int, default=17)
    args = ap.parse_args()
    if args.duration <= 0 or args.fps <= 0 or args.hold < 0:
        ap.error('--duration and --fps must be positive and --hold non-negative')
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            sys.exit(f'{tool} not found on PATH')

    params = DEFAULT.copy()
    if args.params:
        params.update(json.loads(args.params.read_text()))
    for item in args.set:
        key, _, value = item.partition('=')
        if key not in DEFAULT or not value:
            ap.error(f'--set expects KEY=VALUE with KEY in: {", ".join(DEFAULT)}')
        params[key] = float(value)

    with tempfile.TemporaryDirectory(prefix='colorize-') as tmp:
        sources = [resolve(p, args.still, tmp) for p in args.inputs]
        if args.size:
            try:
                w, h = (int(v) for v in args.size.lower().split('x'))
            except ValueError:
                ap.error('--size expects WxH, for example 1080x1920')
        else:
            first = sources[0]
            s = min(1., args.max_edge/max(first['width'], first['height']))
            w, h = first['width']*s, first['height']*s
        w, h = even(w), even(h)
        output = (args.output or Path(f'{args.inputs[0].stem}_colorize.mp4')).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        audio = not args.mute and any(s['audio'] for s in sources)
        renderer = Renderer(w, h)
        if len(sources) == 1:
            render_source(renderer, sources[0], params, args, output, audio)
        else:
            segments = []
            for i, src in enumerate(sources):
                segments.append(Path(tmp)/f'segment_{i:03d}.mp4')
                render_source(renderer, src, params, args, segments[-1], audio)
            listing = Path(tmp)/'segments.txt'
            listing.write_text(''.join(f"file '{p}'\n" for p in segments))
            r = run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', str(listing),
                     '-c', 'copy', '-movflags', '+faststart', str(output)], text=True)
            if r.returncode:
                sys.exit(r.stderr)

    info = probe(output)
    ok = (info['width'], info['height']) == (w, h) and info['duration'] > 0 and info['audio'] == audio
    print(f'VIDEO={w}x{h} {args.fps}fps {info["duration"]:.2f}s audio={"yes" if info["audio"] else "no"}')
    print(f'VALIDATION={"PASS" if ok else "FAIL"}')
    print(f'OUTPUT_VIDEO={output}')
    if not ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
