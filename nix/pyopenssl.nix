{ pyopenssl, fetchPypi }:
pyopenssl.overrideAttrs (old: rec {
  pname = "pyopenssl";
  version = "23.2.0";
  src = fetchPypi {
    inherit pname version;
    sha256 = "sha256-1b4bkcpzhmablf592g21rq3l8apbhklp6wcwlvgfflm4algr6vr7";
  };
})
