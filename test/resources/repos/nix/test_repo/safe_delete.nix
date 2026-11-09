# safe_delete: a `let` variable is seen whole by nixd, an attribute only in its own file.
let
  unused = 1;
  used = 2;
in
{
  value = used;
}
