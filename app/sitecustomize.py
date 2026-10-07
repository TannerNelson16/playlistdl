try:
    import patch_spotapi
    patch_spotapi.apply_patches()
except Exception as e:
    pass
