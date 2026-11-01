from django import template

register = template.Library()


@register.filter
def media_file_url(url):
    """
    Convert a media URL like /media/movies/xxx.jpg to /media-file/movies/xxx
    (without file extension) so Vercel's edge layer does not intercept it
    as a static asset.
    """
    if not url or not isinstance(url, str):
        return url
    if url.startswith('/media/'):
        path = url[7:]  # Remove '/media/'
        # Strip file extension if present
        if '.' in path:
            path = path.rsplit('.', 1)[0]
        return f'/media-file/{path}/'
    return url

