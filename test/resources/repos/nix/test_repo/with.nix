# A name that `with` brings into scope is looked up only when the expression is evaluated, so the source has no
# declaration for it: nixd answers it with the `with` keyword.
let
  helpers = { quad = x: x * 4; };
in
{
  scoped = with helpers; quad 2;
}
