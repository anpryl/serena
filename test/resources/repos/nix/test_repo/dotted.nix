# Attribute paths. A path in one statement is one binding, each segment covering the statement from its own name.
# Statements that share a prefix make one attribute, which no single statement defines.
{
  single.path.only = 1;
  spread.over
    .lines = 2;
  services.web.enable = true;
  services.web.port = 80;
  piece.a = 1;
  between = 2;
  piece.b = 3;
  whole = { inner = 1; };
  whole.extra = 2;
  outer = { inner.a = 1; };
  outer.inner.b = 2;
  inherit ({ fromSet = 1; }) fromSet;
  broken.
    across.leaf = 3;
  late = { };
  late.
    added = 1;
  first.b = 1; other = {
    child = 2;
  };
  inline = { member = 1; };
  commentA = 1; /* first */ commentOther = { /* second */
    child = 2;
  };
}
